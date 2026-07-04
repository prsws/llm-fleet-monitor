# LLM Fleet Monitor

A small, dependency-free tool that answers one question at a glance: **which of my local AI inference hosts are running what, right now?**

Point it at a list of hosts and it reports, per host:

- **Ollama** — version, currently-loaded models with their keep-alive countdown and GPU-vs-CPU residency (it flags models that have *spilled* to CPU), and the downloaded-model inventory.
- **Whisper / Piper** (Wyoming protocol speech services) — program, version, and a summary of available models / voices and languages.
- **Reachability** — up, idle, or unreachable (with a classified reason: timeout, refused, DNS, protocol).

It ships as two pieces:

1. **`llm-fleet-monitor.py`** — the probe. A command-line tool that sweeps the fleet and prints a readable report (or JSON).
2. **`gui.py`** — an optional tiny web dashboard that polls the probe and renders live host cards in the browser.

Both are **pure Python standard library** — no `pip install`, no frameworks.

---

## Requirements

- Python **3.8 or newer**.
- That's it. No third-party packages.

The web dashboard loads [Pico.css](https://picocss.com/) from a CDN at runtime (so the browser needs internet access for styling). **HTMX is not loaded from a CDN — it's vendored locally** in this repo as `htmax.js`; see *Notes* below for why.

---

## Quick start

```bash
git clone https://github.com/<your-user>/llm-fleet-monitor.git
cd llm-fleet-monitor

# 1. Create your host list (see "The host list" below)
cp llm-fleet.csv llm-fleet.csv
$EDITOR llm-fleet.csv

# 2. Run the probe — one-shot readable report
python3 llm-fleet-monitor.py llm-fleet.csv

# 3. (Optional) Run the live web dashboard
python3 gui.py
# then open http://127.0.0.1:8766
```

---

## The host list (CSV)

Both tools read a CSV with a header row. **One row = one endpoint = one service.**

```csv
hostname,description,endpoint,ollama,whisper,piper
gpu-box,"Main Ollama box",192.168.1.20:11434,true,false,false
voice-stt,"Whisper speech-to-text",192.168.1.30:10300,false,true,false
voice-tts,"Piper text-to-speech",192.168.1.30:10200,false,false,true
```

| Column        | Meaning                                                                 |
|---------------|-------------------------------------------------------------------------|
| `hostname`    | A label for the row (shown in the report).                              |
| `description` | A short sentence describing the check. Quote it if it contains commas.  |
| `endpoint`    | `host:port` for this one service. Host may be an IP or DNS name; the port is required. |
| `ollama`      | `true`/`false` — probe this endpoint as Ollama.                         |
| `whisper`     | `true`/`false` — probe this endpoint as a Wyoming Whisper service.      |
| `piper`       | `true`/`false` — probe this endpoint as a Wyoming Piper service.        |

Rules:

- **Exactly one** of `ollama` / `whisper` / `piper` must be `true` per row — that selects how the endpoint is probed. (A single `host:port` addresses one service, so a row with zero or several flags set is skipped with a warning.)
- Booleans are case-insensitive; `true/t/1/yes/y` are truthy, everything else is false.

> **Heads up — don't commit your real host list.** A populated `llm-fleet.csv` is a map of your internal network (IPs, hostnames, which boxes run inference). Keep it out of version control. Ship/commit only a sanitized `llm-fleet.csv.example` with dummy values, and add `llm-fleet.csv` to your `.gitignore`.

---

## Usage — the probe (CLI)

```
python3 llm-fleet-monitor.py HOSTS_CSV [--timeout SECONDS] [--json] [--verbose] [--fail-on-unreachable]
```

| Flag                     | Effect                                                                                  |
|--------------------------|-----------------------------------------------------------------------------------------|
| `HOSTS_CSV`              | Path to the host-list CSV (required).                                                    |
| `--timeout SECONDS`      | Per-endpoint connect+read timeout. Default `3.0`.                                        |
| `--json`                 | Emit the JSON envelope (`schema_version: 1`) to stdout instead of text. Ignores `--verbose`. |
| `--verbose`              | Text mode only: expand the full Whisper/Piper model & voice lists instead of a count.   |
| `--fail-on-unreachable`  | Exit with code `1` if any endpoint was unreachable (useful for cron / monitoring).      |

The probe runs all endpoints **concurrently**, with a per-endpoint timeout, and one dead host never aborts the sweep — unreachable hosts are reported, not fatal.

**Exit codes:**

| Code | Meaning                                                              |
|------|---------------------------------------------------------------------|
| `0`  | Ran and produced a report. (This is the default even if some hosts are down — reporting a down host is success.) |
| `1`  | Only with `--fail-on-unreachable`: at least one endpoint was unreachable. |
| `2`  | The tool itself couldn't run — CSV missing/unreadable, etc.         |

### Example output (text)

```
gpu-box — Main Ollama box
  endpoint   192.168.1.20:11434  [ollama]
  reachable  yes (11 ms)
  version    0.30.6
  loaded (ps):
    gpt-oss:20b      14.1 GB  vram  5.6 GB (39% GPU — SPILLED)  ttl 4m12s
  downloaded (ls): 3 models
    gpt-oss:20b (20.9B MXFP4), llama3.1:8b (8.0B Q4_K_M), qwen2.5:7b (7.6B Q4_K_M)

voice-stt — Whisper speech-to-text
  endpoint   192.168.1.30:10300  [whisper/wyoming]
  reachable  yes (5 ms)
  program    mlx-whisper 1.4.0
  models     whisper-large-v3-turbo [99 langs]
```

`SPILLED` marks a loaded model whose weights don't fully fit in VRAM and have partly fallen back to system RAM — the GPU-vs-CPU split is the signal this tool exists to surface.

### JSON output

`--json` prints a single envelope: `{ "schema_version": 1, "probed_at": "...", "results": [ ... ] }`. This is the stable, machine-readable contract — point dashboards or monitoring at it. The schema is versioned; fields are added, never silently renamed.

---

## Usage — the web dashboard (optional)

```
python3 gui.py [--fixture] [--csv PATH] [--port N]
```

| Flag           | Effect                                                                          |
|----------------|---------------------------------------------------------------------------------|
| `--fixture`    | Serve a captured `sample.json` instead of probing live hosts (handy for trying the UI, or development with no hosts up). |
| `--csv PATH`   | Use a specific host list. Default: `llm-fleet.csv` next to the script.           |
| `--port N`     | Port to bind. Default `8766`.                                                    |

The dashboard binds to `127.0.0.1` (localhost only) and serves three routes:

| Route              | Returns                                                       |
|--------------------|--------------------------------------------------------------|
| `GET /`            | The full page — host cards that auto-refresh.                |
| `GET /fragment/hosts` | Just the cards fragment (what the page polls every 10 s). |
| `GET /status.json` | The raw JSON envelope (the same machine-readable contract as the probe's `--json`). |

How it works: a background thread re-runs the probe every 10 seconds and caches the result in memory; the HTTP handlers only ever read that cache, so a slow or dead host can't hang the page. The browser polls the fragment via HTMX and only repaints when the data actually changes.

---

## How probing works

- **Ollama** is queried over its HTTP API (`/api/version`, `/api/ps`, `/api/tags`) — no SSH, no agent on the host.
- **Whisper / Piper** are queried over the **Wyoming protocol** (TCP) using its `describe`→`info` handshake — the tool asks the service to describe itself and reads back program, version, and available models/voices. No audio is sent.

Everything is read-only network probing. The tool never writes to or changes the hosts it monitors.

To add a provider later, the design is meant to grow by adding a new boolean column and a new probe — the per-row, one-service-per-endpoint shape stays the same.

---

## Notes & known rough edges

- **HTMX is vendored locally, not loaded from a CDN.** The dashboard uses **HTMX 4** (a pre-release/beta at time of writing), served from the `htmax.js` file bundled in this repo rather than from a CDN. Reason: when loaded from the CDN with a pinned Subresource Integrity (SRI) hash, the browser refused to execute the beta build (SRI / cross-origin enforcement — a moving pre-release artifact and a pinned hash don't reliably agree). Serving a vetted local copy sidesteps that entirely and, as a bonus, means the dashboard's interactivity has no external CDN dependency. If you fork this and move to a stable HTMX release, you can switch back to a CDN `<script>` tag with a matching SRI hash, or keep vendoring — both work.
- **`htmax.js` shows up as JavaScript in GitHub's language bar.** That's expected — it's a real vendored library file. To have GitHub classify the repo as Python, add a `.gitattributes` marking it vendored: `htmax.js linguist-vendored`.
- **Default port:** the dashboard serves on **8766**. (An older `--help` string may still say 8765; 8766 is the value the code uses.)
- The dashboard is intentionally tiny and localhost-only. If you want to expose it on a LAN or add authentication, that's on you — it ships with no auth.

---

## Third-party components

- **HTMX** (`htmax.js`, vendored in this repo) — © Big Sky Software, licensed **[0BSD](https://opensource.org/license/0bsd) (Zero-Clause BSD)**. 0BSD is public-domain-equivalent and requires no attribution; this note is provenance, not obligation. Upstream: [htmx.org](https://htmx.org/).
- **Pico.css** (loaded from CDN) — licensed **MIT**. Upstream: [picocss.com](https://picocss.com/).

## License

MIT — see [LICENSE.md](LICENSE.md). Copyright (c) 2026 José F. Reyes-Santana.