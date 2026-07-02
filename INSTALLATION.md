# Install LLM Fleet Monitor on Proxmox VE 9.1 LXC

Installation, configuration, service management, and validation guide

## 1. Purpose and Scope
LLM Fleet Monitor is a small, dependency-free Python tool for answering one operational question quickly: which local AI inference hosts are running what right now. This guide documents how to install and run it inside a Proxmox VE 9.1 Linux Container using only the operating system packages required for Python, Git, networking, and service management.
The deployment described here keeps the monitor isolated in its own LXC, runs the optional dashboard on localhost by default, and uses read-only network probes against Ollama and Wyoming protocol services. No monitored inference host needs an agent, SSH access, or file changes.
## 2.Application Summary
LLM Fleet Monitor ships as two Python scripts:
* llm-fleet-monitor.py — the command-line probe. It reads a CSV host list, probes all endpoints concurrently, and prints either a readable report or JSON output.
* gui.py — an optional lightweight web dashboard. It polls the probe every 10 seconds and renders live host cards in a browser.

The tool monitors Ollama over its HTTP API and Whisper/Piper speech services over the Wyoming protocol. It reports reachability, versions, loaded Ollama models, GPU versus CPU residency, keep-alive countdowns, downloaded model inventory, and available speech models or voices.

## 3. Recommended LXC Design
Use an unprivileged Debian-based LXC. Proxmox containers share the host kernel and are intended for lightweight Linux workloads; unprivileged containers improve isolation by mapping container root to an unprivileged host UID range.

|Setting| Required value                                                              | Notes                                                                                                       |
|---|-----------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------|
|Hostname	| llmdm	                                                                      | Use this exact container hostname.                                                                          |
|Container type	| Unprivileged LXC	                                                           | Preferred for this read-only monitoring workload.                                                           |
|OS template	| debian_trixie.tar from Proxmox VE CT templates	                             | Select the downloaded Debian Trixie container template.                                                     |
|CPU	| 1 core	                                                                     | Sufficient for the lightweight Python probe and dashboard.                                                  |
|Memory	| 512 MB RAM, 512 MB swap	                                                    | The app has no third-party Python dependencies.                                                             |
|Disk	| 8 GB	                                                                       | Provides room for OS packages, Git checkout, logs, and updates.                                             |
|Network	| Static IP <your-ip-here>/24	| Disable the Proxmox firewall for this container.|                                                            |
|DNS	| Use host settings	                                                          | In the CT wizard, keep DNS set to use the Proxmox host configuration.                                       |
|Features	| Nesting not required	                                                       | Enable nesting only if your broader container policy requires it; this app does not run Docker or nested containers. |

## 4. Create the LXC in Proxmox VE 9.1
### 4.1 Create from the Proxmox web interface
0. _Make sure you already have an OS image (e.g. debian-13-standard_13.1-2_amd64.tar.zst) loaded into PVE Templates before you begin._
1. Open the Proxmox VE web interface and select the target node.
2. Select Create CT.
3. Assign hostname llmfm.
4. Use an unprivileged container and enable nesting.
5. Set root password.
6. Select the debian-13-standard_13.1-2_amd64.tar.zst template from the Proxmox VE CT templates.
7. Select where the storage resides in your PVE and set the root disk to 8 GB.
8. Allocate 1 core, 512 MB memory, and 512 MB swap.
9. Configure networking with static IPv4 <your-ip-here>/24, IPv6 static but leave other fields empty and leave the container firewall disabled.
10. Set DNS to Use host settings.
11. Start the container and open its console.

### 4.2 Optional creation from the Proxmox host CLI
Use these values for the CLI example. Adjust only the storage name, bridge, gateway, and CT ID if your Proxmox environment uses different names or numbering.
```
pct create 121 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst --hostname llmfm --unprivileged 1 --cores 1 --memory 512 --swap 512 --rootfs local-lvm:8 --net0 name=eth0,bridge=vmbr0,ip=<your-ip-here>/24,gw=<your-ip-here>,firewall=0 --nameserver host --features nesting=1 --start 1
```
Keep firewall=0 so the container firewall remains disabled, and keep DNS configured to use the host settings.

## 5. Prepare the Container OS
Log in to the LXC console as root, update the base system, and install the small set of packages needed to clone the repository, run Python, and test network reachability.
```
apt update
apt full-upgrade -y
apt install -y  curl git mc htop btop sudo
timedatectl set-timezone America/Puerto_Rico
```
Confirm that Python is version 3.13 or newer (python3 -V). No pip installation, virtual environment, framework, or third-party package is required.

## 6. Install LLM Fleet Monitor
Create a dedicated system user and place the application under /opt.
```
adduser --system --group --home /opt/llm-fleet-monitor llmfm
cd /opt
git clone https://github.com/prsws/llm-fleet-monitor.git
chown -R llmfm:llmfm /opt/llm-fleet-monitor
cd /opt/llm-fleet-monitor
```

## 7. Configure the Host List
The monitor reads a CSV file with one row per service endpoint. A single row must select exactly one service type: Ollama, Whisper, or Piper.
```
cd /opt/llm-fleet-monitor
cp example.llm-fleet.csv llm-fleet.csv
chown llmfm:llmfm llm-fleet.csv
chmod 644 llm-fleet.csv
nano llm-fleet.csv
```
Example CSV:

|hostname	|description	|endpoint	|ollama	|whisper	| piper |
|---|---|---|---|---|---|
|gpu-box	|Main Ollama box	|<your-ip-here>:11434	|true	|false	| false |
|voice-stt	|Whisper speech-to-text	|<your-ip-here>:10300	|false	|true	| false |
|voice-tts	|Piper text-to-speech	|<your-ip-here>:10200	|false	|false	| true |

Keep the real CSV private. It contains internal hostnames, IP addresses, ports, and service roles. Add llm-fleet.csv to .gitignore and commit only sanitized examples.

## 8. Run the CLI Probe
Run a one-shot readable report as the dedicated service user.
```
sudo -u llmfm python3 llm-fleet-monitor.py llm-fleet.csv
```
Useful probe options:

|Command	|Purpose|
|---|---|
| --timeout 5	|Increase per-endpoint connect and read timeout to 5 seconds.|
| --json	|Emit the versioned JSON envelope for dashboards or monitoring systems.|
| --verbose	|Show full Whisper/Piper model and voice lists in text mode.|
| --fail-on-unreachable	|Exit with status code 1 if any endpoint is unreachable.|

## 9. Run the Optional Web Dashboard
Start the dashboard manually for initial testing.
```
cd /opt/llm-fleet-monitor
sudo -u llmfm python3 gui.py --csv /opt/llm-fleet-monitor/llm-fleet.csv --port 8766
```
By default, the dashboard binds to 127.0.0.1:8766 and is reachable only from inside the LXC. The dashboard exposes:

*	/ — full browser page with host cards.
*	/fragment/hosts — host-card fragment polled by the page.
*	/status.json — raw JSON envelope.

If you need to view the dashboard from another workstation, prefer an SSH tunnel from your client to the LXC rather than exposing the dashboard directly, because the app intentionally ships without authentication.
```
ssh -L 8766:127.0.0.1:8766 root@<lxc-ip>
```
Then open http://127.0.0.1:8766 on your workstation. **Not Tested** 

_If you want to expose the dashboard publicly, edit gui.py and change its default host to 0.0.0.0. You can also change the port._ **Tested Ok**


## 10. Create a systemd Service for the Dashboard
Create a service so the dashboard starts automatically when the LXC boots.
```
cat > /etc/systemd/system/llm-fleet-dashboard.service <<'EOF'
[Unit]
Description=LLM Fleet Monitor Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=llmfm
Group=llmfm
WorkingDirectory=/opt/llm-fleet-monitor
ExecStart=/usr/bin/python3 /opt/llm-fleet-monitor/gui.py --csv /opt/llm-fleet-monitor/llm-fleet.csv --port 8766
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
ReadWritePaths=/opt/llm-fleet-monitor

[Install]
WantedBy=multi-user.target
EOF
```
Then enable it:
```
systemctl daemon-reload
systemctl enable --now llm-fleet-dashboard.service
systemctl status llm-fleet-dashboard.service
```
Review logs with:
```
journalctl -u llm-fleet-dashboard.service -f
```

## 11. Optional: Cron-Friendly CLI Monitoring **Not Tested**
If you want a simple scheduled health check, create a wrapper script that exits non-zero when any endpoint is unreachable.
```
cat > /usr/local/bin/llm-fleet-check <<'EOF'
#!/bin/sh
exec /usr/bin/python3 /opt/llm-fleet-monitor/llm-fleet-monitor.py /opt/llm-fleet-monitor/llm-fleet.csv --timeout 3 --fail-on-unreachable
EOF
chmod 755 /usr/local/bin/llm-fleet-check
```
Example cron entry for a five-minute check:
```
*/5 * * * * llmfleet /usr/local/bin/llm-fleet-check >> /var/log/llm-fleet-check.log 2>&1
```

## 12. Network and Firewall Requirements
The LXC must be able to initiate outbound TCP connections to every endpoint listed in the CSV. Typical service ports are:
* Ollama: TCP 11434, unless customized.
* Wyoming Whisper: commonly TCP 10300.
* Wyoming Piper: commonly TCP 10200.
* Dashboard: TCP 8766 only inside the LXC by default because it binds to localhost.

Basic reachability tests from inside the LXC:
```
nc -vz <your-ip-here> 11434
curl -s http://<your-ip-here>:11434/api/version
nc -vz <your-ip-here> 10300
nc -vz <your-ip-here> 10200
```

## 13. Security Notes
* Use an unprivileged LXC unless you have a specific host-resource requirement.
* Do not expose the dashboard to an untrusted network without adding an external authentication and reverse-proxy layer.
* Keep llm-fleet.csv private because it maps internal inference services.
* Do not store secrets in the CSV; the current probes do not require credentials.
* Restrict file permissions so only root and the llmfleet user can read operational files.
* Patch the container regularly with apt update and apt full-upgrade.

# 14. Updating the Application
Update the checked-out repository and restart the dashboard service.
```
cd /opt/llm-fleet-monitor
git pull
chown -R llmfm:llmfm /opt/llm-fleet-monitor
systemctl restart llm-fleet-dashboard.service
systemctl status llm-fleet-dashboard.service
```
Before updating, copy or commit any local changes outside the private CSV. Do not overwrite llm-fleet.csv unless you intentionally want to replace the host list.

## 15. Validation Checklist
* python3 --version reports Python 3.13 or newer.
* llm-fleet.csv exists, has a header row, and each service row has exactly one true provider flag.
* The CLI probe produces a readable report.
* --json produces a single JSON envelope with schema_version, probed_at, and results.
* The dashboard service is active if the optional web UI is enabled.
* curl http://127.0.0.1:8766/status.json returns current status from inside the LXC.
*	Expected unreachable endpoints are classified as timeout, refused, DNS, or protocol rather than crashing the sweep.

## 16. Troubleshooting

|Symptom	| Likely Cause	                                                                                                                          | Action                                                                                                 |
|---|----------------------------------------------------------------------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
|Unable to pull git repo	| Network issues or incorrect SSH key setup.                                                                                             | Ensure the container can reach GitHub over HTTPS and that any required authentication is configured.   |
|CLI probe crashes with ImportError	| Missing Python packages or incompatible versions.                                                                                      | Review and install missing Python libraries from within the LXC.                                       |
|CSV rows are skipped	| Zero or multiple provider flags are true	                                                                                              | Edit the row so exactly one of ollama, whisper, or piper is true.                                      |
|Ollama endpoint is refused	| Ollama is not listening on the listed host and port	                                                                                   | Verify Ollama is running and reachable from the LXC with curl or nc.                                   |
|DNS failure	| Container cannot resolve the hostname	                                                                                                 | Check the LXC DNS settings in Proxmox or use a static IP in the CSV.                                   |
|Dashboard starts but browser looks unstyled	| The browser cannot load Pico.css from the CDN	| Allow browser internet access for styling or accept the unstyled functional page.                      |
|Dashboard not reachable from another machine	| It binds to localhost by design	| Use an SSH tunnel, or intentionally place it behind a secured reverse proxy if LAN access is required. | 
|Service fails after update	| Changed path, permissions, or Python script behavior	                                                                                  | Run the CLI manually as llmfleet, then review journalctl -u llm-fleet-dashboard.service.               |

## 17. Operational Notes
* The monitor is read-only and does not modify the inference hosts it checks.
* A dead or slow host should not abort a sweep; it is reported as unreachable.	
* The JSON schema is versioned. Consumers should read schema_version and tolerate added fields.	
* SPILLED in Ollama output indicates a model is only partially resident in GPU memory and has fallen back to system RAM.	
* HTMX is vendored locally as htmax.js, while Pico.css is loaded by the browser from a CDN at runtime.