# AGENTS: Guide for AI coding agents

Purpose
- Short, actionable orientation for an agent to become productive in this repo.
- The CODE is the source of truth. Where this file and the code disagree, the
  code wins — verify against it before acting.

Big picture
- A small probe + dashboard for monitoring local LLM inference services.
- Two components, pure Python standard library, NO build step, no pip installs:
  - Probe CLI: `llm-fleet-monitor.py` — reads a CSV of hosts, probes providers
    concurrently (Ollama over HTTP; Whisper/Piper over the Wyoming TCP
    protocol; OpenAI-compatible runners, e.g. llama.cpp, over HTTP
    `/v1/models`), normalizes results into a versioned JSON envelope, renders
    text or JSON.
  - Web GUI: `gui.py` — stdlib `ThreadingHTTPServer`. A background thread
    re-runs the probe every REFRESH_SECONDS and caches the envelope; HTTP
    handlers only read the cache. Renders server-side HTML fragments swapped
    by vendored HTMX.

Hard fences — read before editing anything
- `gui.py` contains a block commented "COMMENTED OUT ON PURPOSE BY JOSE ...
  DO NOT MODIFY". Leave it byte-identical.
- `htmax.js` is NOT project code. It is vendored third-party HTMX 4 (beta),
  0BSD-licensed, served locally instead of from a CDN (deliberate — a pinned
  SRI hash against a moving beta CDN artifact broke in-browser). Never edit,
  reformat, or "modernize" it.
- UI copy and layout choices that look unusual are deliberate José edits:
  provider icons in the card header (no `[provider]` text tags in the GUI),
  the "Up?" status wording with arrows, "Running (ps)" labels, the two-line
  card header. Do not revert or normalize them.
- The ptag hash in `envelope_ptag()` deliberately EXCLUDES latency and
  expires_at and buckets ttl_seconds to the minute, so the DOM only repaints
  on real change. Displayed latency going stale between swaps is by design.
  Do not "fix" it by adding volatile fields back.

Key files and entrypoints
- `llm-fleet-monitor.py`
  - `read_rows(csv_path)` — CSV validation. Required columns:
    `sort,hostname,description,endpoint,ollama,whisper,piper,openai` (verify
    the `required` list in code — it grows when providers are added).
    Exactly one provider flag true per row; `sort` must be an integer;
    invalid rows warn and skip; missing columns (including a CSV that
    predates the `openai` column, schema v3+) are a startup error (exit 2).
  - `build_record(row)` — maps provider → probe function. New providers plug
    in here plus a `probe_{provider}` helper returning the same tuple shape.
  - `probe_ollama` — GET `/api/version`, then `/api/ps` and `/api/tags`.
  - `probe_wyoming` — TCP `describe`→`info` handshake (see
    `wyoming_describe`, `extract_whisper_info`, `extract_piper_info`).
  - `run_probe(rows, ...)` — ThreadPoolExecutor sweep; emits the envelope.
    `schema_version` is set here — read the current value from the code, and
    if you change the envelope shape, bump it and update docs + tests.
  - `probe_fleet(csv_path, ...)` — import-safe one-call entrypoint (the GUI
    uses it). Keep it side-effect free.
  - CLI: `python3 llm-fleet-monitor.py HOSTS_CSV [--timeout N] [--json]
    [--verbose] [--fail-on-unreachable]` — the CSV is POSITIONAL (only
    gui.py has a `--csv` flag; do not confuse the two).
  - Exit codes are a REPORTER model: 0 = ran and reported, even if hosts are
    down (a down host is a finding, not a failure); 1 only with
    `--fail-on-unreachable`; 2 = the tool itself couldn't run. Preserve this.

- `gui.py`
  - `python3 gui.py [--csv PATH] [--port N] [--fixture]` — binds
    127.0.0.1:8766 by default. `--fixture` serves `sample.json` instead of
    probing (use it to exercise the server with no live hosts).
  - Routes: `/` (page), `/fragment/hosts` (polled cards fragment),
    `/status.json` (raw envelope), static: `/htmax.js`, `/style.css`,
    `/images/*`.
  - Rendering: server-side HTML strings; every dynamic value goes through
    `html_escape()`. Accordions carry stable ids (`ld-{slug}`, `dl-{slug}`)
    and the Downloaded accordion is `hx-preserve`d so open state and content
    survive swaps — stable ids are load-bearing, keep them.
  - `import_probe_module(path)` loads the probe by file path (the filename
    has hyphens, so normal `import` cannot work — importlib only).

Developer workflow
- Run everything from the REPO ROOT.
- Tests: `python3 -m unittest` (discovery finds `tests/test_*.py`; expect a
  nonzero test count in the output — if it says "Ran 0 tests" something is
  wrong, do not treat it as green).
- Tests load the modules by path (`load_monitor_module` / `load_gui_module`):
  keep both files import-safe — no top-level side effects, no network at
  import time, all CLI behavior under `if __name__ == "__main__":`.
- After changing probe or rendering behavior: run the tests, then start
  `python3 gui.py --fixture` and eyeball `/` and `/status.json`.
- Paths in code resolve relative to the script file
  (`Path(__file__).resolve().parent`), never the working directory. Follow
  that pattern for any new file access.

Conventions
- Absent data is `null`, never `""`, throughout the envelope; renderers omit
  rows/lines for null fields rather than printing placeholders.
- Human-readable sizes/TTLs via the existing `human_size` / `human_ttl`
  helpers (note: the CLI and GUI ttl formats intentionally differ slightly).
- Pico.css variables only in `style.css` — no hardcoded colors/px spacing.
- Docs live in `README.md` (public contract: CSV format, flags, exit codes,
  JSON schema) and `INSTALLATION.md` (LXC deployment). If your change alters
  behavior they describe, update them in the same change — doc drift is the
  most common defect in this repo's history.

References
- CSV rules and flags: `llm-fleet-monitor.py` (read_rows, main), `README.md`
- Provider probes: `llm-fleet-monitor.py` (probe_ollama, probe_wyoming)
- GUI swap/ptag behavior: `gui.py` (envelope_ptag, render_cards_fragment)
- Tests: `tests/test_llm_fleet_monitor.py`, `tests/test_gui.py`