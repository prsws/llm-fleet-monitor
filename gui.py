#!/usr/bin/env python3
"""
Tiny stdlib-only GUI for llm-fleet-monitor.

Hard rules obeyed:
- All paths resolved relative to this file (no cwd use).
- Probe imported by absolute path via importlib (filename has a dash).
- Background thread performs probing on a fixed cadence; handlers only read cache.
- Do one probe at startup so the first page has data.
- HTTP implemented with http.server.ThreadingHTTPServer + BaseHTTPRequestHandler.

Routes:
  GET /                Full HTML page (Pico.css + HTMX 4 + initial cards)
  GET /fragment/hosts  Cards fragment only (for HTMX polling)
  GET /status.json     Cached envelope as JSON

Flags:
  --fixture            Serve HERE/sample.json instead of probing (dev fixture)
  --csv PATH           Override CSV (default HERE/llm-fleet.csv)
  --port N             Override port (default 8765)

Run:
  python gui.py --fixture
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import signal
import sys
import threading
import time
from hashlib import sha256
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional


# ------------------------- Config -------------------------

HERE = Path(__file__).resolve().parent
DEFAULT_CSV = HERE / "llm-fleet.csv"
PROBE_PATH = HERE / "llm-fleet-monitor.py"
FIXTURE_JSON = HERE / "sample.json"

HOST = "127.0.0.1"
DEFAULT_PORT = 8766
REFRESH_SECONDS = 10


# ------------------------- Probe Import -------------------------

def import_probe_module(path: Path):
    spec = importlib.util.spec_from_file_location("fleet_probe", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load probe module from {path}")
    mod = importlib.util.module_from_spec(spec)
    # Ensure the module is visible in sys.modules during execution so that
    # decorators (e.g., @dataclass) and typing.get_type_hints can resolve
    # the module context correctly. Without this, some Python versions may
    # throw errors like: "'NoneType' object has no attribute '__dict__'".
    sys.modules[spec.name] = mod  # type: ignore[index]
    spec.loader.exec_module(mod)  # type: ignore[assignment]
    return mod


# ------------------------- Cache -------------------------

_cache_lock = threading.RLock()
_cached_envelope: Dict[str, Any] = {"schema_version": 1, "probed_at": None, "results": []}


def set_envelope(env: Dict[str, Any]) -> None:
    with _cache_lock:
        # store a deep-ish copy to avoid mutation surprises
        _cached_envelope.clear()
        _cached_envelope.update(json.loads(json.dumps(env)))


def get_envelope() -> Dict[str, Any]:
    with _cache_lock:
        return json.loads(json.dumps(_cached_envelope))


def envelope_ptag(env: Dict[str, Any]) -> str:
    # Stable tag for hx-ptag extension; hash the results content
    s = json.dumps({"results": env.get("results", [])}, sort_keys=True, separators=(",", ":"))
    return sha256(s.encode("utf-8")).hexdigest()


# ------------------------- Humanizers -------------------------

def human_size(n: Optional[int]) -> str:
    if n is None:
        return "?"
    gb = 1_000_000_000
    mb = 1_000_000
    if n >= gb:
        return f"{n/gb:.1f} GB"
    if n >= mb:
        return f"{n/mb:.1f} MB"
    return f"{n} B"


def human_ttl(ttl_seconds: Any) -> str:
    if ttl_seconds is None:
        return "?"
    # The probe emits the string "forever" for permanently-loaded models
    # (keep_alive = -1); preserve that instead of coercing it to int.
    if ttl_seconds == "forever":
        return "forever"
    try:
        s = int(ttl_seconds)
    except Exception:
        return "?"
    if s < 0:
        return "0s"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}h{m}m"
    if m > 0:
        return f"{m}m{sec}s"
    return f"{sec}s"


# ------------------------- Rendering -------------------------

def render_cards_fragment(env: Dict[str, Any]) -> str:
    tag = envelope_ptag(env)
    parts: List[str] = []
    parts.append(f'<div id="host-cards" hx-ptag="{tag}">')

    results = env.get("results") or []
    if not results:
        parts.append("<p>No hosts to display.</p>")
    else:
        for rec in results:
            hostname = rec.get("hostname", "?")
            description = rec.get("description", "")
            provider = rec.get("provider", "?")
            endpoint = rec.get("endpoint", "?")
            reachable = bool(rec.get("reachable"))
            latency_ms = rec.get("latency_ms")
            err = rec.get("error") or None

            parts.append("<article>")
            # Header
            parts.append(f"<header><strong>{html_escape(hostname)}</strong> — {html_escape(description)}</header>")
            # Endpoint
            tag_txt = {"ollama": "ollama", "whisper": "whisper/wyoming", "piper": "piper/wyoming"}.get(provider, provider)
            parts.append(f"<small><code>{html_escape(endpoint)}</code> [{html_escape(tag_txt)}]</small>")

            # Reachability
            if reachable:
                lat = f"{int(latency_ms)} ms" if isinstance(latency_ms, int) else "?"
                parts.append(f"<p><mark>reachable</mark> yes ({lat})</p>")
            else:
                if err:
                    kind = html_escape(str(err.get("kind")))
                    detail = html_escape(str(err.get("detail") or ""))
                    dash = f" — {detail}" if detail else ""
                    parts.append(f"<p><mark>reachable</mark> NO — {kind}{dash}</p>")
                else:
                    parts.append(f"<p><mark>reachable</mark> NO</p>")

            # Provider-specific blocks
            if provider == "ollama" and rec.get("ollama"):
                o = rec["ollama"] or {}
                ver = (o.get("version") or "").strip()
                if ver:
                    parts.append(f"<p><strong>version</strong> {html_escape(ver)}</p>")

                loaded = o.get("loaded") or []
                if loaded:
                    parts.append("<details open><summary>loaded (ps)</summary><ul>")
                    for m in loaded:
                        name = html_escape(str(m.get("name") or "?"))
                        size = human_size(m.get("size"))
                        vram = human_size(m.get("size_vram"))
                        gpu_frac = m.get("gpu_fraction")
                        spilled = isinstance(gpu_frac, (int, float)) and gpu_frac < 1.0
                        gpu_pct = f"{int(round(gpu_frac * 100))}%" if isinstance(gpu_frac, (int, float)) else "?"
                        ttl = human_ttl(m.get("ttl_seconds")) if m.get("ttl_seconds") is not None else "?"
                        spill_note = " — <strong>SPILLED</strong>" if spilled else ""
                        parts.append(
                            f"<li><code>{name}</code> {size} • vram {vram} ({gpu_pct} GPU){spill_note} • ttl {ttl}</li>"
                        )
                    parts.append("</ul></details>")
                else:
                    parts.append("<p>up, nothing loaded</p>")

                inv = o.get("downloaded") or []
                parts.append(f"<p>downloaded (ls): {len(inv)} model{'s' if len(inv)!=1 else ''}</p>")
                if inv:
                    names: List[str] = []
                    for m in inv:
                        nm = html_escape(str(m.get("name") or "?"))
                        ps = (m.get("parameter_size") or "").strip() if isinstance(m.get("parameter_size"), str) else m.get("parameter_size")
                        q = (m.get("quantization") or "").strip() if isinstance(m.get("quantization"), str) else m.get("quantization")
                        fam = (m.get("family") or "").strip() if isinstance(m.get("family"), str) else m.get("family")
                        suffix_parts = [html_escape(p) for p in [ps, q, fam] if isinstance(p, str) and p]
                        suffix = f" ({' '.join(suffix_parts)})" if suffix_parts else ""
                        names.append(f"{nm}{suffix}")
                    parts.append(f"<p style=\"max-width: 80ch;\">{', '.join(names)}</p>")

            elif provider == "whisper" and rec.get("whisper"):
                w = rec["whisper"] or {}
                prog = (w.get("program") or "").strip()
                ver = (w.get("version") or "").strip()
                if prog or ver:
                    parts.append(f"<p><strong>program</strong> {html_escape((prog + ' ' + ver).strip())}</p>")
                models = w.get("models") or []
                if models:
                    count_langs = 0
                    for m in models:
                        count_langs += len(m.get("languages") or [])
                    parts.append(f"<p>{len(models)} models across ~{count_langs} langs</p>")

            elif provider == "piper" and rec.get("piper"):
                p = rec["piper"] or {}
                prog = (p.get("program") or "").strip()
                ver = (p.get("version") or "").strip()
                if prog or ver:
                    parts.append(f"<p><strong>program</strong> {html_escape((prog + ' ' + ver).strip())}</p>")
                voices = p.get("voices") or []
                if voices:
                    n = len(voices)
                    lang_set = set()
                    for v in voices:
                        for lg in (v.get("languages") or []):
                            if isinstance(lg, str) and lg:
                                lang_set.add(lg)
                    m = len(lang_set)
                    parts.append(f"<p>{n} voices across {m} langs</p>")

            parts.append("</article>")

    parts.append("</div>")
    return "\n".join(parts)


def render_full_page(env: Dict[str, Any]) -> str:
    fragment = render_cards_fragment(env)
    # Pico.css and HTMX 4 (exact pin) + requested extensions
    pico_css = "https://unpkg.com/@picocss/pico@2.0.6/css/pico.min.css"
    # COMMENTED OUT ON PURPOSE BY JOSE 20260630 - replace htmx4 cdn with local downloaded htmax.js - DO NOT MODIFY
    # htmx_src = (
    #     "https://cdn.jsdelivr.net/npm/htmx.org@4.0.0-beta5"
    # )
    # htmx_sri = (
    #     "sha384-5dnhUXCt1hXGvYrjAnKwgNX3I8xtIJiW6eIHIbeo7oWyXv2XpWYC/rl+ZiWfuYO5"
    # )
    htmx_src = (
        "/htmax.js"
    )
    htmx_sri = (
        ""
    )

    page = f"""
<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\"> 
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>LLM Fleet Monitor</title>
    <link rel=\"stylesheet\" href=\"{pico_css}\">
<!--
COMMENTED OUT ON PURPOSE BY JOSE 20260630 - replace htmx4 cdn with local downloaded htmax.js - DO NOT MODIFY
    <script src=\"{htmx_src}\" integrity=\"{htmx_sri}\" crossorigin=\"anonymous\"></script>
-->
    <script src=\"{htmx_src}\" crossorigin=\"anonymous\"></script>
  </head>
  <body> 
    <main class=\"container\">
      <h1>LLM Fleet Monitor</h1>
      <div id=\"cards-root\" 
           hx-ext=\"browser-indicator,ptag\" 
           hx-get=\"/fragment/hosts\" 
           hx-trigger=\"every {REFRESH_SECONDS}s\" 
           hx-swap=\"innerHTML\">
        {fragment}
      </div>
      <p><small>&copy; 2026 José F. Reyes Santana. Polling every {REFRESH_SECONDS}s via HTMX 4.</small></p>
    </main>
  </body>
 </html>
"""
    return page


def html_escape(s: Any) -> str:
    t = str(s)
    return (
        t.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )


# ------------------------- HTTP -------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "LLMFleetGUI/1.0"

    def do_GET(self) -> None:  # noqa: N802
        try:
            if self.path == "/":
                self._send_html(HTTPStatus.OK, render_full_page(get_envelope()))
            elif self.path == "/fragment/hosts":
                self._send_html(HTTPStatus.OK, render_cards_fragment(get_envelope()))
            elif self.path == "/status.json":
                env = get_envelope()
                body = json.dumps(env).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/htmax.js":
                body = (HERE / "htmax.js").read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._send_html(HTTPStatus.NOT_FOUND, "<h3>Not Found</h3>")
        except Exception as e:
            # Convert unexpected errors into an intentional HTML error page (never raw traceback)
            self._send_html(HTTPStatus.INTERNAL_SERVER_ERROR, f"<h3>Server error</h3><p>{html_escape(e)}</p>")

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        # Keep logs quiet unless explicitly needed
        sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), format % args))

    def _send_html(self, status: HTTPStatus, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ------------------------- Background Probe -------------------------

def _probe_once_with(mod, csv_path: Path, timeout: float = 3.0) -> Dict[str, Any]:
    # We call probe_fleet() — the importable function we added.
    # Function name used: probe_fleet
    func = getattr(mod, "probe_fleet", None)
    if not callable(func):
        # Fallback wrapper in case the module predates the refactor
        rows_fn = getattr(mod, "read_rows", None)
        run_fn = getattr(mod, "run_probe", None)
        if callable(rows_fn) and callable(run_fn):
            rows, _warns = rows_fn(str(csv_path))
            return run_fn(rows, timeout=float(timeout))
        raise RuntimeError("probe module does not expose required API")
    return func(str(csv_path), timeout=float(timeout))


def start_probe_thread(mod, csv_path: Path, stop_evt: threading.Event) -> None:
    def loop() -> None:
        while not stop_evt.is_set():
            try:
                env = _probe_once_with(mod, csv_path)
                set_envelope(env)
            except Exception as e:
                # On error, keep previous envelope and try again later
                sys.stderr.write(f"probe error: {e}\n")
            # Sleep in small slices to react faster to shutdown
            for _ in range(int(REFRESH_SECONDS * 10)):
                if stop_evt.is_set():
                    break
                time.sleep(0.1)

    t = threading.Thread(target=loop, name="probe-thread", daemon=True)
    t.start()


# ------------------------- Main -------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="LLM Fleet Monitor — tiny stdlib GUI")
    p.add_argument("--fixture", action="store_true", help="Serve HERE/sample.json instead of probing")
    p.add_argument("--csv", type=str, default=str(DEFAULT_CSV), help="Path to llm-fleet.csv (default: HERE/llm-fleet.csv)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind (default 8765)")
    args = p.parse_args(argv)

    csv_path = Path(args.csv).resolve()

    if args.fixture:
        # Fixture mode: serve sample.json (or an empty envelope fallback)
        try:
            with open(FIXTURE_JSON, "r", encoding="utf-8") as f:
                env = json.load(f)
        except FileNotFoundError:
            # Graceful fallback if sample.json is absent
            env = {"schema_version": 1, "probed_at": None, "results": []}
        set_envelope(env)
        mod = None
    else:
        # Enforce CSV presence at the resolved path (one-line message, no traceback)
        if not csv_path.exists():
            print(f"CSV not found: {csv_path}")
            return 2
        # Import probe by absolute path
        try:
            mod = import_probe_module(PROBE_PATH)
        except Exception as e:
            print(f"failed to import probe: {PROBE_PATH} — {e}")
            return 2
        # Initial probe before serving so first page isn't empty
        try:
            env = _probe_once_with(mod, csv_path)
            set_envelope(env)
        except Exception as e:
            # Don't crash the server on initial failure; show zero-state page
            sys.stderr.write(f"initial probe failed: {e}\n")
            set_envelope({"schema_version": 1, "probed_at": None, "results": []})

    # Start background refresher if not in fixture mode
    stop_evt = threading.Event()
    if not args.fixture and mod is not None:
        start_probe_thread(mod, csv_path, stop_evt)

    # HTTP server
    srv = ThreadingHTTPServer((HOST, int(args.port)), Handler)
    # Ensure request handler threads don't block process exit on shutdown
    try:
        srv.daemon_threads = True  # type: ignore[attr-defined]
    except Exception:
        pass
    sa = srv.socket.getsockname()
    print(f"Serving on http://{sa[0]}:{sa[1]}")

    # Graceful shutdown on SIGINT/SIGTERM (best-effort).
    # NOTE: srv.shutdown() blocks until serve_forever() returns, so it must NOT be
    # called from the signal handler — handlers run on the main thread, which is
    # exactly where serve_forever() is blocked, producing a deadlock. Run it on a
    # separate thread so the main thread can unwind out of serve_forever().
    def _shutdown(*_a):
        stop_evt.set()
        threading.Thread(target=srv.shutdown, name="shutdown", daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        try:
            if sig is not None:
                signal.signal(sig, _shutdown)
        except Exception:
            pass

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        # Graceful Ctrl-C shutdown like any other program
        print("\nShutting down…")
        try:
            _shutdown()
        except Exception:
            pass
    finally:
        stop_evt.set()
        try:
            srv.server_close()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
