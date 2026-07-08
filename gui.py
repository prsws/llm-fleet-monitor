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
  --port N             Override port (default 8766)

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

HOST = "127.0.0.1"      # use 0.0.0.0 for external access (unsecure)
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
_cached_envelope: Dict[str, Any] = {"schema_version": 3, "probed_at": None, "results": []}


def set_envelope(env: Dict[str, Any]) -> None:
    with _cache_lock:
        # store a deep-ish copy to avoid mutation surprises
        _cached_envelope.clear()
        _cached_envelope.update(json.loads(json.dumps(env)))


def get_envelope() -> Dict[str, Any]:
    with _cache_lock:
        return json.loads(json.dumps(_cached_envelope))


def envelope_ptag(env: Dict[str, Any]) -> str:
    """
    Produce a stable page tag for the current fleet results, suitable for the
    htmx ptag extension to suppress no-op swaps.

    WHY: Raw results include volatile fields that change nearly every poll
    (`latency_ms`, per-model `expires_at`, and `ttl_seconds` ticking down),
    which would defeat change detection. We therefore compute the hash over a
    NORMALIZED deep copy that:
      - removes `latency_ms` from each host result;
      - removes `expires_at` from each loaded-model entry;
      - replaces each loaded-model `ttl_seconds` with a bucketed `ttl_bucket`:
          * the string "forever" stays "forever";
          * numeric becomes int(ttl_seconds)//60 (minutes floor);
          * missing/None stays None.
    All other fields (reachability, errors, versions, loaded model names/sizes,
    GPU fraction, downloaded inventory, whisper/piper blocks) remain as-is in
    the hash input. This yields stable tags within the same TTL minute, and tag
    changes on real state transitions or TTL minute rollovers.
    """
    # Start from a JSON round-trip to deep-copy without mutating the cache
    src_results = json.loads(json.dumps(env.get("results", [])))

    def _ttl_bucket(val: Any):
        if val is None:
            return None
        if val == "forever":
            return "forever"
        try:
            return int(val) // 60
        except Exception:
            return None

    norm_results: List[Dict[str, Any]] = []
    for rec in src_results:
        if not isinstance(rec, dict):
            norm_results.append(rec)
            continue
        r = dict(rec)
        # Remove volatile host-level latency
        r.pop("latency_ms", None)

        # Provider-specific normalization
        prov = r.get("provider")
        if prov == "ollama" and isinstance(r.get("ollama"), dict):
            o = dict(r["ollama"])  # type: ignore[index]
            # Normalize loaded models
            loaded = []
            for m in o.get("loaded") or []:
                if not isinstance(m, dict):
                    loaded.append(m)
                    continue
                mm = dict(m)
                mm.pop("expires_at", None)
                if "ttl_seconds" in mm:
                    mm["ttl_bucket"] = _ttl_bucket(mm.get("ttl_seconds"))
                    mm.pop("ttl_seconds", None)
                else:
                    mm["ttl_bucket"] = _ttl_bucket(None)
                loaded.append(mm)
            o["loaded"] = loaded
            r["ollama"] = o
        # whisper/piper blocks are kept as-is

        norm_results.append(r)

    s = json.dumps({"results": norm_results}, sort_keys=True, separators=(",", ":"))
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
        def _slugify(h: str) -> str:
            # Lowercase and replace non-alphanumerics with '-'
            out = []
            for ch in (h or "").lower():
                if ("a" <= ch <= "z") or ("0" <= ch <= "9"):
                    out.append(ch)
                else:
                    out.append("-")
            # Collapse consecutive dashes
            slug = "".join(out)
            while "--" in slug:
                slug = slug.replace("--", "-")
            return slug.strip("-") or "host"

        for rec in results:
            hostname = rec.get("hostname", "?")
            description = rec.get("description", "")
            provider = rec.get("provider", "?")
            endpoint = rec.get("endpoint", "?")
            reachable = bool(rec.get("reachable"))
            latency_ms = rec.get("latency_ms")
            err = rec.get("error") or None
            slug = _slugify(str(hostname))

            parts.append("<article>")
            # Header (split hostname and description into two lines within header)
            # Determine provider icon (if any)
            icon_file = {
                "ollama": "ollama.png",
                "whisper": "Whisper.png",
                "piper": "Piper.png",
                "openai": "OpenAI.png",
            }.get(provider or "")

            # Pico.css-friendly header with a small icon next to the title
            if icon_file:
                icon_tag = (
                    f"<img src=\"/images/{icon_file}\" alt=\"{html_escape(provider)} icon\" "
                    "width=\"28\" height=\"28\" loading=\"lazy\" style=\"vertical-align:middle;margin-right:.5rem;\">"
                )
            else:
                icon_tag = ""

            parts.append(
                "<header class=\"host-card-header\">"
                f"<div class=\"host-title\">{icon_tag}<strong>{html_escape(hostname)}</strong></div>"
                f"<div class=\"host-desc\">{html_escape(description)}</div>"
                "</header>"
            )
            # Endpoint + Reachability as a compact 2-column table (no <mark>, shared style with model tables)
            tag_txt = {"ollama": "ollama", "whisper": "whisper/wyoming", "piper": "piper/wyoming"}.get(provider, provider)
            # Build status cell content preserving existing wording except bold NO
            if reachable:
                lat = f"{int(latency_ms)} ms" if isinstance(latency_ms, int) else "?"
                status_html = f"&#x2B06; Yes ({lat})"
            else:
                if err:
                    kind = html_escape(str(err.get("kind")))
                    detail = html_escape(str(err.get("detail") or ""))
                    dash = f" — {detail}" if detail else ""
                    status_html = f"<strong>&#x2B07; NO</strong> — {kind}{dash}"
                else:
                    status_html = "<strong>&#x2B07; NO</strong>"

            parts.append(
                "<table class=\"model-table status-table\">"
                "<thead><tr><th class=\"property-name\">Endpoint</th><th class=\"property-name\">Status</th></tr></thead>"
                "<tbody>"
                f"<tr><td class=\"property-value\">{html_escape(endpoint)}</td><td class=\"property-value\">{status_html}</td></tr>"
                "</tbody></table>"
            )

            # Provider-specific blocks (only when host is reachable)
            if reachable and provider == "ollama" and rec.get("ollama"):
                o = rec["ollama"] or {}
                ver = (o.get("version") or "").strip()
                if ver:
                    parts.append(f"<span class=\"property-name indent-span\">Version</span> <span class=\"property-value\">{html_escape(ver)}</span>")

                loaded = o.get("loaded") or []
                if loaded:
                    # Loaded: do NOT preserve element content (TTLs must update on real swaps).
                    # Provide a stable id to allow Idiomorph to match elements across swaps.
                    # Per htmx 4 beta5 docs (see /docs.md under "Swapping" and Idiomorph),
                    # using the morph swap allows attribute-preserving matching by id.
                    parts.append(f"<br />")
                    parts.append(f"<details id=\"ld-{slug}\" open><summary><span class=\"property-name indent-span\">Running models (ps):</span> <span class=\"property-value\">{len(loaded)}</span></summary>")
                    for m in loaded:
                        name = html_escape(str(m.get("name") or "?"))
                        size = human_size(m.get("size"))
                        vram = human_size(m.get("size_vram"))
                        gpu_frac = m.get("gpu_fraction")
                        spilled = isinstance(gpu_frac, (int, float)) and gpu_frac < 1.0
                        gpu_pct = f"{int(round(gpu_frac * 100))}%" if isinstance(gpu_frac, (int, float)) else "?"
                        ttl = human_ttl(m.get("ttl_seconds")) if m.get("ttl_seconds") is not None else "?"
                        spill_note = " — <span class=\"red-blink\">SPILLED</span> &#129751;" if spilled else ""
                        parts.append(
                            "<table class=\"model-table\">"
                            f"<thead><tr><th colspan=\"2\" class=\"property-name-alt\">{name}</th></tr></thead>"
                            "<tbody>"
                            f"<tr><td class=\"property-name\">&#128207; Size</td><td class=\"property-value\">{size}</td></tr>"
                            f"<tr><td class=\"property-name\">&#x1F40F; VRAM</td><td class=\"property-value\">{vram} ({gpu_pct} GPU){spill_note}</td></tr>"
                            f"<tr><td class=\"property-name\">&#9201; TTL</td><td class=\"property-value\">{ttl}</td></tr>"
                            "</tbody></table>"
                        )
                    parts.append("</details>")
                else:
                    parts.append("<div><span class=\"property-name indent-span\">Running models (ps):</span> <span class=\"property-value\">&#x2B06; Up, nothing running</span></div>")

                inv = o.get("downloaded") or []
                # Consolidate downloaded count into the accordion header. When empty, show a plain line.
                # NOTE: Count lives inside an hx-preserve'd <details>, so it freezes across swaps (updates on page load).
                if inv:
                    # Downloaded: preserve the element to keep open/closed state across swaps.
                    # Per htmx 4 beta5 docs (/docs.md "Preserving Elements Across Swaps"),
                    # adding the `hx-preserve` attribute keeps the existing element instance.
#                    parts.append(f"<hr />")
                    parts.append(f"<details id=\"dl-{slug}\" hx-preserve><summary><span class=\"property-name indent-span\">Downloaded models (ls):</span> <span class=\"property-value\">{len(inv)}</span></summary>")

                    for m in inv:
                        nm = html_escape(str(m.get("name") or "?"))
                        # Build rows conditionally; size is always present in downloaded entries
                        rows: List[str] = []
                        param = m.get("parameter_size")
                        if isinstance(param, str) and param.strip():
                            rows.append(f"<tr><td>Parameters</td><td>{html_escape(param.strip())}</td></tr>")
                        quant = m.get("quantization")
                        if isinstance(quant, str) and quant.strip():
                            rows.append(f"<tr><td>Quantization</td><td>{html_escape(quant.strip())}</td></tr>")
                        fam = m.get("family")
                        if isinstance(fam, str) and fam.strip():
                            rows.append(f"<tr><td>Family</td><td>{html_escape(fam.strip())}</td></tr>")
                        size_h = human_size(m.get("size"))
                        rows.append(f"<tr><td>Size</td><td>{size_h}</td></tr>")

                        parts.append(
                            "<table class=\"model-table\">"
                            f"<thead><tr><th colspan=\"2\"><code>{nm}</code></th></tr></thead>"
                            "<tbody>"
                            + "".join(rows) +
                            "</tbody></table>"
                        )
                    parts.append("</details>")
                else:
                    parts.append("<p><strong>Downloaded models</strong> (ls): 0</p>")

            elif reachable and provider == "whisper" and rec.get("whisper"):
                w = rec["whisper"] or {}
                prog = (w.get("program") or "").strip()
                ver = (w.get("version") or "").strip()
                if prog or ver:
                    parts.append(f"<p><strong>Version</strong> {html_escape((prog + ' ' + ver).strip())}</p>")
                models = w.get("models") or []
                if models:
                    count_langs = 0
                    for m in models:
                        count_langs += len(m.get("languages") or [])
                    parts.append(f"<p>{len(models)} models across ~{count_langs} langs</p>")

            elif reachable and provider == "piper" and rec.get("piper"):
                p = rec["piper"] or {}
                prog = (p.get("program") or "").strip()
                ver = (p.get("version") or "").strip()
                if prog or ver:
                    parts.append(f"<p><strong>Version</strong> {html_escape((prog + ' ' + ver).strip())}</p>")
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

            elif reachable and provider == "openai" and rec.get("openai"):
                o = rec["openai"] or {}
                server = (o.get("server") or "").strip()
                if server:
                    parts.append(f"<p><strong>Server</strong> {html_escape(server)}</p>")
                models = o.get("models") or []
                if models:
                    # Accordion with hx-preserve
                    parts.append(
                        f"<details id=\"oa-{slug}\" hx-preserve><summary><strong>Downloaded models</strong> (v1): {len(models)}</summary>"
                    )
                    for m in models:
                        model_id = html_escape(str(m.get("id") or "?"))
                        owned_by = m.get("owned_by")
                        # Build table rows
                        rows: List[str] = [f"<tr><td colspan=\"2\"><code>{model_id}</code></td></tr>"]
                        if owned_by:
                            rows.append(f"<tr><td>owned_by</td><td>{html_escape(str(owned_by))}</td></tr>")
                        parts.append(
                            "<table class=\"model-table\">"
                            "<tbody>"
                            + "".join(rows) +
                            "</tbody></table>"
                        )
                    parts.append("</details>")
                else:
                    parts.append(f"<p><strong>Available models (v1)</strong>: 0</p>")

            parts.append("</article>")

    parts.append("</div>")
    return "\n".join(parts)


def render_full_page(env: Dict[str, Any]) -> str:
    fragment = render_cards_fragment(env)
    # Pico.css and HTMX 4 (exact pin) + requested extensions
    pico_css = "https://unpkg.com/@picocss/pico@2.0.6/css/pico.min.css"
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
    <link rel=\"stylesheet\" href=\"/style.css\">
<!--
COMMENTED OUT ON PURPOSE BY JOSE 20260630 - replace htmx4 cdn with local downloaded htmax.js - DO NOT MODIFY
    <script src=\"{htmx_src}\" integrity=\"{htmx_sri}\" crossorigin=\"anonymous\"></script>
-->
    <script src=\"{htmx_src}\" crossorigin=\"anonymous\"></script>
  </head>
  <body> 
    <main class=\"container\">
      <h1 class=\"page-title\">LLM Fleet Monitor</h1>
      <div id=\"cards-root\" 
           hx-ext=\"browser-indicator,ptag\" 
           hx-get=\"/fragment/hosts\" 
           hx-trigger=\"every {REFRESH_SECONDS}s\" 
           hx-indicator=\"#scan-indicator\"
           hx-swap=\"innerMorph\">
        {fragment}
      </div>
      <!-- htmx 4 beta5: use innerMorph (Idiomorph) to preserve element instances by id; docs: https://four.htmx.org/docs ("Morph Swaps").
           Preserve Downloaded accordions with hx-preserve; docs: https://htmx.org/attributes/hx-preserve -->
      <div id=\"scan-indicator\" aria-live=\"polite\"><span class=\"dot\" aria-hidden=\"true\"></span> <small>scanning…</small></div>
      <p><small>Polling every {REFRESH_SECONDS}s. Powered by Python, HTMX4 & PicoCSS.</small></p>
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
            elif self.path == "/style.css":
                body = (HERE / "style.css").read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/css; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/images/"):
                # Serve icons from HERE/images (png only)
                name = Path(self.path).name
                img_path = (HERE / "images" / name)
                if img_path.exists() and img_path.suffix.lower() == ".png":
                    body = img_path.read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._send_html(HTTPStatus.NOT_FOUND, "<h3>Not Found</h3>")
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
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind (default 8766)")
    args = p.parse_args(argv)

    csv_path = Path(args.csv).resolve()

    if args.fixture:
        # Fixture mode: serve sample.json (or an empty envelope fallback)
        try:
            with open(FIXTURE_JSON, "r", encoding="utf-8") as f:
                env = json.load(f)
        except FileNotFoundError:
            # Graceful fallback if sample.json is absent
            env = {"schema_version": 3, "probed_at": None, "results": []}
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
            set_envelope({"schema_version": 3, "probed_at": None, "results": []})

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
