#!/usr/bin/env python3
"""
LLM Fleet Monitor — phase 1 (readable text output)

Spec summary:
 - Reads a CSV of endpoints (one endpoint per row), validates exactly one provider flag.
 - Probes Ollama (HTTP) or Wyoming Whisper/Piper (TCP) concurrently with per-endpoint timeout.
 - Builds JSON-shaped records (future-friendly), renders text now; optional --json output.
 - Classifies errors; one bad host never aborts the sweep.

Stdlib-only implementation (urllib, socket, csv, concurrent.futures, argparse, datetime, json).
"""

from __future__ import annotations

import argparse
import csv
import json
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Set
from urllib.error import URLError, HTTPError
from urllib.parse import urlunparse
from urllib.request import Request, urlopen


# ------------------------- Utilities -------------------------


def iso_now_seconds() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_bool(val: str) -> bool:
    if val is None:
        return False
    s = str(val).strip().lower()
    if s in ("true", "t", "1", "yes", "y"):  # accept a few extras
        return True
    if s in ("false", "f", "0", "no", "n", ""):
        return False
    return False


def human_size(n_bytes: Optional[int]) -> str:
    if n_bytes is None:
        return "?"
    # Use decimal GB/MB for readability similar to sample
    gb = 1_000_000_000
    mb = 1_000_000
    if n_bytes >= gb:
        return f"{n_bytes/gb:.1f} GB"
    if n_bytes >= mb:
        return f"{n_bytes/mb:.1f} MB"
    return f"{n_bytes} B"


def human_ttl(ttl_seconds: Any) -> str:
    if ttl_seconds == "forever":
        return "forever"
    try:
        n = int(max(0, int(ttl_seconds)))
    except Exception:
        return "?"
    m, s = divmod(n, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def classify_error(exc: BaseException) -> Tuple[str, str]:
    # kind, detail
    if isinstance(exc, socket.timeout):
        return "timeout", str(exc)
    if isinstance(exc, TimeoutError):
        return "timeout", str(exc)
    if isinstance(exc, ConnectionRefusedError):
        return "refused", str(exc)
    if isinstance(exc, socket.gaierror):
        return "dns", str(exc)
    if isinstance(exc, HTTPError):
        # Check for auth errors first (401/403)
        if exc.code in (401, 403):
            return "auth", "endpoint requires an API key (not supported)"
        # HTTP connected but provider error or wrong path → protocol
        return "protocol", f"HTTP {exc.code}: {exc.reason}"
    if isinstance(exc, URLError):
        # URLError wraps many reasons, including timeout
        reason = getattr(exc, 'reason', None)
        if isinstance(reason, socket.timeout):
            return "timeout", str(exc)
        if isinstance(reason, ConnectionRefusedError):
            return "refused", str(exc)
        if isinstance(reason, socket.gaierror):
            return "dns", str(exc)
        return "other", str(exc)
    if isinstance(exc, json.JSONDecodeError):
        return "protocol", str(exc)
    if isinstance(exc, ConnectionError):
        # The TCP connection succeeded but the peer dropped mid-exchange —
        # e.g. wyoming_describe() raises a bare ConnectionError when the
        # socket closes before a full describe/info message arrives.
        # ConnectionRefusedError (never connected) is handled above and
        # keeps its "refused" classification; everything else in the
        # ConnectionError family lands here as a protocol-level failure,
        # since the connection itself was established. This is the sole
        # home for that case now — probe_wyoming no longer re-classifies
        # it locally.
        return "protocol", str(exc)
    return "other", str(exc)


def parse_host_port(endpoint: str) -> Tuple[str, int]:
    host, sep, port = endpoint.rpartition(":")
    if not sep:
        raise ValueError(f"endpoint missing port: {endpoint}")
    return host, int(port)


# ------------------------- Probes -------------------------


def http_get_json(host: str, port: int, path: str, timeout: float) -> Dict[str, Any]:
    url = urlunparse(("http", f"{host}:{port}", path, "", "", ""))
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


def http_get_json_with_headers(host: str, port: int, path: str, timeout: float) -> Tuple[Dict[str, Any], Optional[str]]:
    """Get JSON and return (data, Server header value or None)"""
    url = urlunparse(("http", f"{host}:{port}", path, "", "", ""))
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        server = resp.headers.get("Server")
    return json.loads(data.decode("utf-8")), server


def _norm(v: Any) -> Optional[str]:
    """Normalize absent/blank metadata to None (null in JSON).

    Ollama may omit or provide empty strings for fields like
    parameter_size, quantization(_level), and family — especially for
    MLX-converted models. Represent any such "no value" uniformly as
    None so machine consumers see a single sentinel.
    """
    return v if v else None


def probe_ollama(host: str, port: int, timeout: float) -> Tuple[bool, Optional[int], Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    start = time.perf_counter()
    latency_ms: Optional[int] = None
    result: Dict[str, Any] = {"version": None, "loaded": [], "downloaded": []}
    # Reachability: version is the canary
    try:
        ver = http_get_json(host, port, "/api/version", timeout)
        latency_ms = int((time.perf_counter() - start) * 1000)
        result["version"] = ver.get("version")
        reachable = True
    except Exception as e:
        kind, detail = classify_error(e)
        return False, None, None, {"kind": kind, "detail": detail}

    # ps — loaded models
    try:
        ps = http_get_json(host, port, "/api/ps", timeout)
        now = datetime.now(timezone.utc)
        models = []
        for m in ps.get("models", []) or []:
            name = m.get("name") or m.get("model")
            size = m.get("size")
            size_vram = m.get("size_vram")
            expires_at = m.get("expires_at")
            ttl: Any = None
            if expires_at:
                try:
                    # Handle timezone-aware, assume ISO8601
                    exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    diff = (exp - now).total_seconds()
                    # Treat absurdly large as forever (keep_alive = -1)
                    if diff > 5 * 365 * 24 * 3600:
                        ttl = "forever"
                    else:
                        ttl = max(0, int(diff))
                except Exception:
                    ttl = None
            details = m.get("details") or {}
            gpu_fraction = None
            try:
                if size and size_vram is not None and size:
                    gpu_fraction = round(float(size_vram) / float(size), 2)
            except Exception:
                gpu_fraction = None
            models.append({
                "name": name,
                "size": size,
                "size_vram": size_vram,
                "gpu_fraction": gpu_fraction,
                "expires_at": expires_at,
                "ttl_seconds": ttl,
                # Coalesce absent metadata consistently to None
                "parameter_size": _norm(details.get("parameter_size")),
                "quantization": _norm(details.get("quantization_level") or details.get("quantization")),
            })
        result["loaded"] = models
    except Exception:
        # Degrade gracefully — keep version, continue
        pass

    # tags — downloaded inventory
    try:
        tags = http_get_json(host, port, "/api/tags", timeout)
        inv = []
        for m in tags.get("models", []) or []:
            details = m.get("details") or {}
            inv.append({
                "name": m.get("name"),
                # Normalize all optional metadata fields to None if absent/blank
                "parameter_size": _norm(details.get("parameter_size")),
                "quantization": _norm(details.get("quantization_level") or details.get("quantization")),
                "family": _norm(details.get("family")),
                "size": m.get("size"),
            })
        result["downloaded"] = inv
    except Exception:
        pass

    return True, latency_ms, result, None


def wyoming_describe(host: str, port: int, timeout: float = 3.0) -> Dict[str, Any]:
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall((json.dumps({"type": "describe"}) + "\n").encode("utf-8"))
        buf = b""

        def read_until_newline() -> bytes:
            nonlocal buf
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    raise ConnectionError("closed before info")
                buf += chunk
            line, buf2 = buf.split(b"\n", 1)
            buf = buf2
            return line

        def read_n(n: int) -> bytes:
            nonlocal buf
            while len(buf) < n:
                chunk = s.recv(4096)
                if not chunk:
                    raise ConnectionError("closed mid-event")
                buf += chunk
            out, buf2 = buf[:n], buf[n:]
            buf = buf2
            return out

        while True:
            header = json.loads(read_until_newline().decode("utf-8"))
            data = json.loads(read_n(header["data_length"]).decode("utf-8")) if header.get("data_length") else {}
            if header.get("payload_length"):
                read_n(header["payload_length"])  # discard
            if header.get("type") == "info":
                return data


def extract_whisper_info(info: Dict[str, Any]) -> Dict[str, Any]:
    # Wyoming servers may vary slightly; aim for robust extraction.
    asr_list = info.get("asr") or []
    program = None
    version = None
    models: List[Dict[str, Any]] = []
    if isinstance(asr_list, list) and asr_list:
        p = asr_list[0]
        program = p.get("name")
        version = p.get("version")
        for m in p.get("models", []) or []:
            models.append({
                "name": m.get("name"),
                "languages": m.get("languages") or m.get("language") or [],
            })
    return {"program": program, "version": version, "models": models}


def extract_piper_info(info: Dict[str, Any]) -> Dict[str, Any]:
    tts_list = info.get("tts") or []
    program = None
    version = None
    voices: List[Dict[str, Any]] = []
    if isinstance(tts_list, list) and tts_list:
        p = tts_list[0]
        program = p.get("name")
        version = p.get("version")
        for v in p.get("voices", []) or []:
            voices.append({
                "name": v.get("name"),
                "languages": v.get("languages") or v.get("language") or [],
            })
    return {"program": program, "version": version, "voices": voices}


def probe_wyoming(host: str, port: int, timeout: float, kind: str) -> Tuple[bool, Optional[int], Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    # kind: "whisper" or "piper"
    start = time.perf_counter()
    try:
        info = wyoming_describe(host, port, timeout=timeout)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if kind == "whisper":
            return True, latency_ms, extract_whisper_info(info), None
        elif kind == "piper":
            return True, latency_ms, extract_piper_info(info), None
        else:
            return True, latency_ms, {"note": "unknown kind"}, None
    except Exception as e:
        # classify_error() is the single source of truth for error kind here
        # now — no wyoming-specific override. ConnectionRefusedError (never
        # connected) -> "refused"; a bare ConnectionError (handshake dropped
        # mid-exchange, raised by wyoming_describe above) -> "protocol".
        # Both are handled inside classify_error() itself.
        kind_s, detail = classify_error(e)
        return False, None, None, {"kind": kind_s, "detail": detail}


def probe_openai(host: str, port: int, timeout: float) -> Tuple[bool, Optional[int], Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    start = time.perf_counter()
    try:
        data, server = http_get_json_with_headers(host, port, "/v1/models", timeout)
        latency_ms = int((time.perf_counter() - start) * 1000)

        # Parse models: keep id and owned_by (null when absent/empty)
        models = []
        for entry in data.get("data", []) or []:
            model_id = entry.get("id")
            if model_id:  # Skip entries without id
                owned_by = entry.get("owned_by")
                # Normalize empty string to None
                if owned_by == "":
                    owned_by = None
                models.append({
                    "id": model_id,
                    "owned_by": owned_by,
                })

        result = {
            "server": server,
            "models": models,
        }
        return True, latency_ms, result, None
    except Exception as e:
        kind, detail = classify_error(e)
        return False, None, None, {"kind": kind, "detail": detail}


# Prometheus family-name fragments worth diffing. Permissive on purpose: the
# build may rename families, and an unmatched counter still shows up in
# `families` so it is diagnosable without another round trip.
_COUNTER_HINTS = ("request", "prompt_token", "generation_token", "completion_token")
# Histogram families we summarise as mean = _sum / _count.
_HISTOGRAM_HINTS = ("latency", "duration", "ttft", "time_to_first_token")


def http_get_text(host: str, port: int, path: str, timeout: float) -> Tuple[str, int]:
    url = urlunparse(("http", f"{host}:{port}", path, "", "", ""))
    req = Request(url, headers={"Accept": "text/plain"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace"), resp.status


def parse_prometheus(body: str) -> Dict[str, float]:
    """Prometheus text exposition -> {family_name: summed_value}.

    Labels are dropped and values summed per family. We want one number per
    family to diff across polls, not a label cube. Histogram `_sum` / `_count`
    survive as their own families, which is what summarise_histograms needs.
    Malformed lines are skipped.
    """
    out: Dict[str, float] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        brace = line.find("{")
        if brace != -1:
            end = line.find("}", brace)
            if end == -1:
                continue
            name, rest = line[:brace], line[end + 1:]
        else:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            name, rest = parts
        try:
            val = float(rest.strip().split()[0])
        except (ValueError, IndexError):
            continue
        out[name] = out.get(name, 0.0) + val
    return out


def summarise_histograms(families: Dict[str, float]) -> Dict[str, float]:
    """Mean = _sum / _count for each histogram family.

    Not a percentile. Bucket interpolation across a 10 s poll would be noise;
    the lifetime mean is a stable, honest number, and diffing successive
    (_sum, _count) pairs in gui.py yields the per-interval mean.
    """
    means: Dict[str, float] = {}
    for name, total in families.items():
        if not name.endswith("_sum"):
            continue
        base = name[:-4]
        if not any(h in base.lower() for h in _HISTOGRAM_HINTS):
            continue
        count = families.get(base + "_count", 0.0)
        if count > 0:
            means[base] = total / count
    return means


def _try_get_text(host: str, port: int, path: str, timeout: float):
    """GET returning (body, None) or (None, error_string). Never raises."""
    try:
        body, status = http_get_text(host, port, path, timeout)
        if status == 200:
            return body, None
        return None, f"status {status}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _try_get_json(host: str, port: int, path: str, timeout: float):
    """GET JSON returning (obj, None) or (None, error_string). Never raises."""
    body, err = _try_get_text(host, port, path, timeout)
    if body is None:
        return None, err
    try:
        return json.loads(body), None
    except Exception as e:
        return None, f"bad json: {e}"


# READ-ONLY routes only. rapid-mlx also exposes /v1/cache/clear,
# /v1/cache/import and /v1/requests/{id}/cancel -- these MUTATE server state.
# Never add them to a probe, health check, or dashboard poll.
_RO_STATUS = "/v1/status"
_RO_MODELS = "/v1/models"
_RO_HEALTH = "/health"      # confirmed present; /v1/health is NOT (404)
_RO_METRICS = "/metrics"


def probe_rapidmlx(
    host: str, port: int, timeout: float
) -> Tuple[bool, Optional[int], Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
    """Probe a rapid-mlx server.

    Liveness + inventory -> /v1/models  (the one route every build serves)
    Activity + cache + memory -> /v1/status

    /v1/status carries `total_requests_processed`, a monotonic counter. That is
    the LATCH: a 3 s inference beginning and ending between two 10 s polls still
    increments it. rapid-mlx keeps models resident forever, so it has no
    keep-alive TTL and no loaded/unloaded transition -- this counter is the only
    way a poller can see the server do work.

    It also reports metal.active/peak_memory_gb, which is authoritative. Do not
    infer rapid-mlx's footprint from a process RSS column: on macOS, per-process
    accounting does not attribute Metal buffers back to the owning process.
    """
    start = time.perf_counter()
    try:
        data, server = http_get_json_with_headers(host, port, _RO_MODELS, timeout)
        latency_ms = int((time.perf_counter() - start) * 1000)
    except Exception as e:
        kind, detail = classify_error(e)
        return False, None, None, {"kind": kind, "detail": detail}

    models: List[Dict[str, Any]] = []
    for entry in data.get("data", []) or []:
        model_id = entry.get("id")
        if not model_id:
            continue
        models.append({
            "id": model_id,
            "owned_by": _norm(entry.get("owned_by")),
            "tool_call_parser": _norm(entry.get("tool_call_parser")),
            "reasoning_parser": _norm(entry.get("reasoning_parser")),
            "is_hybrid": bool(entry.get("is_hybrid")),
            "is_moe": bool(entry.get("is_moe")),
            "supports_spec_decode": bool(entry.get("supports_spec_decode")),
        })

    health = None
    hbody, _ = _try_get_text(host, port, _RO_HEALTH, timeout)
    if hbody is not None:
        health = hbody.strip()[:120]

    status_obj, status_err = _try_get_json(host, port, _RO_STATUS, timeout)
    activity: Optional[Dict[str, Any]] = None
    if status_obj:
        cache = status_obj.get("cache") or {}
        metal = status_obj.get("metal") or {}
        activity = {
            "state": status_obj.get("status"),
            "model": status_obj.get("model"),
            "uptime_s": status_obj.get("uptime_s"),
            # The latch. Diff across polls in gui.py.
            "total_requests_processed": status_obj.get("total_requests_processed"),
            "total_prompt_tokens": status_obj.get("total_prompt_tokens"),
            "total_completion_tokens": status_obj.get("total_completion_tokens"),
            "num_running": status_obj.get("num_running"),
            "num_waiting": status_obj.get("num_waiting"),
            "generation_tps": status_obj.get("generation_tps"),
            "prompt_tps": status_obj.get("prompt_tps"),
            "cache": {
                "hits": cache.get("hits"),
                "misses": cache.get("misses"),
                "hit_rate": cache.get("hit_rate"),
                "evictions": cache.get("evictions"),
                "entry_count": cache.get("entry_count"),
                "tokens_saved": cache.get("tokens_saved"),
                "memory_utilization": cache.get("memory_utilization"),
            },
            "metal": {
                "active_memory_gb": metal.get("active_memory_gb"),
                "peak_memory_gb": metal.get("peak_memory_gb"),
            },
        }

    result = {
        "server": server,
        # Resident for the life of the process. No TTL, no eviction, no VRAM
        # spill (unified memory). Residency is a server property, not per-model.
        "residency": "resident (no eviction)",
        "health": health,
        "models": models,
        "model_count": len(models),
        "activity": activity,
        "activity_unavailable": status_err if activity is None else None,
    }
    return True, latency_ms, result, None


def _pct(v):
    return "—" if v is None else f"{v * 100:.1f}%"


def render_rapidmlx(rec: Dict[str, Any], verbose: bool) -> List[str]:
    d = rec.get("rapidmlx") or {}
    lines: List[str] = []
    if d.get("server"):
        lines.append(f"  server     {d['server']}")
    lines.append(f"  residency  {d.get('residency', 'unknown')}")

    models = d.get("models") or []
    lines.append(f"  serving    {len(models)} model(s)")
    shown = models if verbose else models[:3]
    for m in shown:
        badges = []
        if m.get("is_hybrid"):
            # Upstream #163: on hybrid models the prefix cache stores entries
            # but never serves them.
            badges.append("HYBRID — prefix cache suspect (#163)")
        if m.get("is_moe"):
            badges.append("MoE")
        if m.get("supports_spec_decode"):
            badges.append("spec-decode")
        parser = m.get("tool_call_parser") or "—"
        reason = m.get("reasoning_parser") or "—"
        lines.append(f"    {m['id']}  tools:{parser}  reasoning:{reason}")
        if badges:
            lines.append(f"      {'  '.join(badges)}")
    if not verbose and len(models) > 3:
        lines.append(f"    … and {len(models) - 3} more")

    a = d.get("activity")
    if not a:
        why = d.get("activity_unavailable") or "unknown"
        lines.append(f"  activity   UNAVAILABLE — {why}")
        lines.append("             no request counter; escalation not observable by polling")
        return lines

    up = a.get("uptime_s")
    up_txt = f"{up / 3600:.1f}h" if isinstance(up, (int, float)) else "—"
    lines.append(f"  state      {a.get('state') or '—'}   uptime {up_txt}")
    lines.append(
        f"  requests   {a.get('total_requests_processed')} processed"
        f"   running {a.get('num_running')}   waiting {a.get('num_waiting')}"
    )
    pt, ct = a.get("total_prompt_tokens"), a.get("total_completion_tokens")
    if isinstance(pt, int) and isinstance(ct, int):
        ratio = f"{pt / ct:.0f}:1" if ct else "—"
        lines.append(f"  tokens     prompt {pt:,}  completion {ct:,}  ({ratio} prefill-heavy)")

    c = a.get("cache") or {}
    if c.get("hits") is not None:
        line = (f"  cache      hit {_pct(c.get('hit_rate'))}"
                f"  ({c.get('hits')}h/{c.get('misses')}m)"
                f"  entries {c.get('entry_count')}  evictions {c.get('evictions')}")
        lines.append(line)
        util = c.get("memory_utilization")
        ev, ent = c.get("evictions"), c.get("entry_count")
        warn = []
        if isinstance(util, float) and util > 0.85:
            warn.append(f"mem {_pct(util)} full")
        if isinstance(ev, int) and isinstance(ent, int) and ev > ent:
            warn.append("evicting more than it holds")
        if warn:
            lines.append(f"             THRASHING — {'; '.join(warn)}")
        ts = c.get("tokens_saved")
        if isinstance(ts, int):
            lines.append(f"             tokens saved {ts:,}")

    m = a.get("metal") or {}
    if m.get("active_memory_gb") is not None:
        lines.append(
            f"  metal      active {m['active_memory_gb']:.2f} GB"
            f"   peak {m.get('peak_memory_gb', 0):.2f} GB"
        )
    return lines


# ------------------------- CSV & Orchestration -------------------------


@dataclass
class Row:
    sort: int
    hostname: str
    description: str
    endpoint: str
    provider: str  # "ollama" | "whisper" | "piper" | "openai" | "rapidmlx"


def read_rows(csv_path: str) -> Tuple[List[Row], List[str]]:
    rows: List[Row] = []
    warnings: List[str] = []
    try:
        with open(csv_path, newline='', encoding='utf-8') as f:
            rdr = csv.DictReader(f)
            required = ["sort", "hostname", "description", "endpoint", "ollama", "whisper", "piper", "openai", "rapidmlx"]
            missing = [c for c in required if c not in (rdr.fieldnames or [])]
            if missing:
                raise ValueError(f"CSV missing required column(s): {', '.join(missing)}")
            for i, r in enumerate(rdr, start=2):  # 1-based with header, so first row is line 2
                try:
                    hostname = (r.get("hostname") or "").strip()
                    endpoint = (r.get("endpoint") or "").strip()
                    # Parse sort after hostname is available for better warnings
                    sort_raw = r.get("sort")
                    try:
                        sort = int((sort_raw or "").strip())
                    except ValueError:
                        warnings.append(
                            f"line {i} ({hostname or 'unknown'}): bad sort value '{sort_raw}' — skipping"
                        )
                        continue
                    hostname = (r.get("hostname") or "").strip()
                    description = (r.get("description") or "").strip()
                    if not hostname or not endpoint:
                        warnings.append(f"line {i}: missing hostname or endpoint — skipping")
                        continue
                    flags = {
                        "ollama": parse_bool(r.get("ollama")),
                        "whisper": parse_bool(r.get("whisper")),
                        "piper": parse_bool(r.get("piper")),
                        "openai": parse_bool(r.get("openai")),
                        "rapidmlx": parse_bool(r.get("rapidmlx")),
                    }
                    true_flags = [k for k, v in flags.items() if v]
                    if len(true_flags) != 1:
                        warnings.append(
                            f"line {i} ({hostname}): expected exactly one provider flag true; got {true_flags or 'none'} — skipping")
                        continue
                    provider = true_flags[0]
                    # Validate endpoint format early
                    try:
                        parse_host_port(endpoint)
                    except Exception as e:
                        warnings.append(f"line {i} ({hostname}): bad endpoint '{endpoint}': {e} — skipping")
                        continue
                    rows.append(Row(sort=sort, hostname=hostname, description=description, endpoint=endpoint, provider=provider))
                except Exception as e:
                    warnings.append(f"line {i}: error parsing row: {e} — skipping")
    except FileNotFoundError:
        raise
    except Exception:
        raise
    return rows, warnings


def build_record(row: Row, timeout: float) -> Dict[str, Any]:
    host, port = parse_host_port(row.endpoint)
    base: Dict[str, Any] = {
        "sort": row.sort,
        "hostname": row.hostname,
        "description": row.description,
        "endpoint": row.endpoint,
        "provider": row.provider,
        "reachable": False,
        "latency_ms": None,
        "error": None,
        "ollama": None,
        "whisper": None,
        "piper": None,
        "openai": None,
        "rapidmlx": None,
    }
    if row.provider == "ollama":
        ok, latency, data, err = probe_ollama(host, port, timeout)
        base["reachable"] = ok
        base["latency_ms"] = latency
        base["error"] = err
        base["ollama"] = data if ok else None
    elif row.provider in ("whisper", "piper"):
        ok, latency, data, err = probe_wyoming(host, port, timeout, row.provider)
        base["reachable"] = ok
        base["latency_ms"] = latency
        base["error"] = err
        base[row.provider] = data if ok else None
    elif row.provider == "openai":
        ok, latency, data, err = probe_openai(host, port, timeout)
        base["reachable"] = ok
        base["latency_ms"] = latency
        base["error"] = err
        base["openai"] = data if ok else None
    elif row.provider == "rapidmlx":
        ok, latency, data, err = probe_rapidmlx(host, port, timeout)
        base["reachable"] = ok
        base["latency_ms"] = latency
        base["error"] = err
        base["rapidmlx"] = data if ok else None
    else:
        base["error"] = {"kind": "other", "detail": f"unknown provider {row.provider}"}
    return base


def run_probe(rows: List[Row], timeout: float, max_workers: int = 16) -> Dict[str, Any]:
    probed_at = iso_now_seconds()
    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        fut_map = {ex.submit(build_record, r, timeout): r for r in rows}
        for fut in as_completed(fut_map):
            try:
                rec = fut.result()
            except Exception as e:
                # This should not happen as build_record/probes should catch and classify internally,
                # but guard anyway.
                r = fut_map[fut]
                rec = {
                    "sort": r.sort,
                    "hostname": r.hostname,
                    "description": r.description,
                    "endpoint": r.endpoint,
                    "provider": r.provider,
                    "reachable": False,
                    "latency_ms": None,
                    "error": {"kind": "other", "detail": f"internal: {e}"},
                    "ollama": None,
                    "whisper": None,
                    "piper": None,
                    "openai": None,
                    "rapidmlx": None,
                }
            results.append(rec)
    # Sort results deterministically by (sort, endpoint). endpoint, not
    # hostname, is the actual per-row unique key (README: "one row = one
    # endpoint = one service") — hostname is just a display label and is
    # expected to repeat across rows for the same physical box.
    results.sort(key=lambda rec: (rec.get("sort", 0), rec.get("endpoint") or ""))
    # Phase 2A: add stable top-level schema version for machine-consumable output
    return {"schema_version": 4, "probed_at": probed_at, "results": results}


# ------------------------- Importable API -------------------------


def probe_fleet(csv_path: str, *, timeout: float = 3.0, max_workers: int = 16) -> Dict[str, Any]:
    """
    Import-safe function that reads the CSV at csv_path and returns the envelope dict.

    This is the single entry-point used by external importers (e.g. a GUI) and by the CLI
    to build the JSON envelope. Importing this module must have no side effects.
    """
    rows, _warns = read_rows(csv_path)
    return run_probe(rows, timeout=timeout, max_workers=max_workers)


# ------------------------- Rendering -------------------------


def render_text(envelope: Dict[str, Any], *, verbose: bool = False) -> str:
    lines: List[str] = []
    for rec in envelope.get("results", []):
        hostname = rec.get("hostname")
        description = rec.get("description")
        provider = rec.get("provider")
        endpoint = rec.get("endpoint")
        reachable = rec.get("reachable")
        latency_ms = rec.get("latency_ms")
        err = rec.get("error") or None
        # Header
        lines.append(f"{hostname} — {description}")
        # Endpoint line
        tag = {
            "ollama": "ollama",
            "whisper": "whisper/wyoming",
            "piper": "piper/wyoming",
            "openai": "openai",
            "rapidmlx": "rapid-mlx",
        }.get(provider, provider)
        lines.append(f"  endpoint   {endpoint}  [{tag}]")
        # Reachability
        if reachable:
            lines.append(f"  reachable  yes ({latency_ms} ms)")
        else:
            if err:
                kind = err.get("kind")
                detail = err.get("detail")
                # Try to spot timeout seconds from detail; otherwise print generic
                lines.append(f"  reachable  NO — {kind}{' — ' + detail if detail else ''}")
            else:
                lines.append("  reachable  NO")

        # Provider-specific blocks
        if provider == "ollama" and rec.get("ollama"):
            o = rec["ollama"]
            if o.get("version"):
                lines.append(f"  version    {o['version']}")
            # Loaded models
            loaded = o.get("loaded") or []
            if loaded:
                lines.append("  loaded (ps):")
                for m in loaded:
                    name = m.get("name") or "?"
                    size = human_size(m.get("size"))
                    vram = human_size(m.get("size_vram"))
                    gpu_frac = m.get("gpu_fraction")
                    spilled = (isinstance(gpu_frac, (int, float)) and gpu_frac < 1.0)
                    gpu_pct = f"{int(round(gpu_frac * 100))}%" if isinstance(gpu_frac, (int, float)) else "?"
                    ttl = human_ttl(m.get("ttl_seconds")) if m.get("ttl_seconds") is not None else "?"
                    spill_note = " — SPILLED" if spilled else ""
                    lines.append(
                        f"    {name:16s} {size:>8s}  vram {vram:>6s} ({gpu_pct} GPU{spill_note})  ttl {ttl}")
            else:
                lines.append("  loaded (ps): none")

            # Downloaded inventory
            inv = o.get("downloaded") or []
            lines.append(f"  downloaded (ls): {len(inv)} model{'s' if len(inv)!=1 else ''}")
            if inv:
                # summarize names and (param size, quant) when present
                names = []
                for m in inv:
                    nm = m.get("name") or "?"
                    ps = m.get("parameter_size")
                    q = m.get("quantization")
                    # Ollama leaves parameter_size/quantization_level empty for MLX-converted
                    # models — the metadata is absent from the manifest, not a parsing miss.
                    # Rendering a bare name here is the truthful result.
                    parts = [p for p in ((ps or "").strip(), (q or "").strip()) if p]
                    suffix = f" ({' '.join(parts)})" if parts else ""
                    names.append(f"{nm}{suffix}")
                # Limit line length; join all for now
                lines.append("    " + ", ".join(names))

        elif provider == "whisper" and rec.get("whisper"):
            w = rec["whisper"]
            prog = (w.get("program") or "").strip()
            ver = (w.get("version") or "").strip()
            if prog or ver:
                lines.append(f"  program    {prog} {ver}".rstrip())
            models = w.get("models") or []
            if models:
                for i, m in enumerate(models):
                    nm = m.get("name") or "?"
                    if verbose:
                        langs = ", ".join(m.get("languages") or [])
                        text = f"{nm} [{langs}]"
                    else:
                        k = len(m.get("languages") or [])
                        text = f"{nm} [{k} langs]"
                    if i == 0:
                        lines.append(f"  models     {text}")
                    else:
                        lines.append(f"             {text}")

        elif provider == "piper" and rec.get("piper"):
            p = rec["piper"]
            prog = (p.get("program") or "").strip()
            ver = (p.get("version") or "").strip()
            if prog or ver:
                lines.append(f"  program    {prog} {ver}".rstrip())
            voices = p.get("voices") or []
            if voices:
                if verbose:
                    for i, v in enumerate(voices):
                        nm = v.get("name") or "?"
                        langs = ", ".join(v.get("languages") or [])
                        if i == 0:
                            lines.append(f"  voices     {nm} [{langs}]")
                        else:
                            lines.append(f"             {nm} [{langs}]")
                else:
                    # Summarize: N installed across M langs
                    n = len(voices)
                    # Count distinct languages across all voices (flatten list safely)
                    lang_set: Set[str] = set()
                    for v in voices:
                        for lg in (v.get("languages") or []):
                            if isinstance(lg, str) and lg:
                                lang_set.add(lg)
                    m = len(lang_set)
                    lines.append(f"  voices     {n} installed across {m} langs")

        elif provider == "openai" and rec.get("openai"):
            o = rec["openai"]
            server = (o.get("server") or "").strip()
            if server:
                lines.append(f"  server     {server}")
            models = o.get("models") or []
            if models:
                # Format: comma-joined ids on wrapped lines, count in header
                lines.append(f"  models     {len(models)} available")
                if verbose:
                    for m in models:
                        model_id = m.get("id") or "?"
                        owned_by = m.get("owned_by")
                        text = f"{model_id}"
                        if owned_by:
                            text += f" ({owned_by})"
                        lines.append(f"    {text}")
                else:
                    # Default: comma-joined ids on wrapped lines
                    ids = [m.get("id") or "?" for m in models]
                    lines.append("    " + ", ".join(ids))

        elif provider == "rapidmlx" and rec.get("rapidmlx"):
            lines.extend(render_rapidmlx(rec, verbose))

        lines.append("")  # blank line between blocks
    return "\n".join(lines).rstrip() + "\n"


# ------------------------- CLI -------------------------


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="llm_fleet_monitor.py", description="Probe a fleet of LLM/voice endpoints and report status.")
    p.add_argument("hosts_csv", help="Path to hosts CSV with columns: sort,hostname,description,endpoint,ollama,whisper,piper,openai")
    p.add_argument("--timeout", type=float, default=3.0, help="Per-endpoint connect+read timeout in seconds (default 3.0)")
    p.add_argument("--json", dest="as_json", action="store_true", help="Emit the JSON envelope (schema_version=3) to stdout; silently ignores --verbose")
    p.add_argument("--verbose", action="store_true", help="Text view only: show full Whisper/Piper/OpenAI details; ignored with --json")
    p.add_argument("--fail-on-unreachable", action="store_true", help="Exit 1 if any endpoint is unreachable")

    args = p.parse_args(argv)

    try:
        rows, warns = read_rows(args.hosts_csv)
    except FileNotFoundError:
        print(f"error: CSV not found: {args.hosts_csv}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: failed to read CSV: {e}", file=sys.stderr)
        return 2

    for w in warns:
        print(f"warning: {w}", file=sys.stderr)

    try:
        # Build the envelope via the shared API function to avoid duplicating probe logic
        envelope = probe_fleet(args.hosts_csv, timeout=float(args.timeout))
    except Exception as e:
        print(f"error: unexpected failure during probe: {e}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(envelope, indent=2))
    else:
        print(render_text(envelope, verbose=bool(args.verbose)), end="")

    exit_code = 0
    if args.fail_on_unreachable:
        any_unreach = any(not r.get("reachable") for r in envelope.get("results", []))
        if any_unreach:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())