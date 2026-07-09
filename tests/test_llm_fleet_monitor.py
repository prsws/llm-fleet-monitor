# tests/test_llm_fleet_monitor.py
from __future__ import annotations

import csv
import importlib.util
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm-fleet-monitor.py"


def load_monitor_module():
    spec = importlib.util.spec_from_file_location("llm_fleet_monitor", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None

    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


monitor = load_monitor_module()


class TestLlmFleetMonitorUtilities(unittest.TestCase):
    def test_parse_bool_accepts_common_truthy_and_falsey_values(self):
        self.assertTrue(monitor.parse_bool("true"))
        self.assertTrue(monitor.parse_bool("YES"))
        self.assertTrue(monitor.parse_bool("1"))

        self.assertFalse(monitor.parse_bool("false"))
        self.assertFalse(monitor.parse_bool("0"))
        self.assertFalse(monitor.parse_bool(""))
        self.assertFalse(monitor.parse_bool("not-a-bool"))

    def test_human_size(self):
        self.assertEqual(monitor.human_size(None), "?")
        self.assertEqual(monitor.human_size(999), "999 B")
        self.assertEqual(monitor.human_size(1_500_000), "1.5 MB")
        self.assertEqual(monitor.human_size(2_000_000_000), "2.0 GB")

    def test_human_ttl(self):
        self.assertEqual(monitor.human_ttl("forever"), "forever")
        self.assertEqual(monitor.human_ttl(5), "5s")
        self.assertEqual(monitor.human_ttl(65), "1m05s")
        self.assertEqual(monitor.human_ttl(3661), "1h01m01s")
        self.assertEqual(monitor.human_ttl("bad"), "?")

    def test_parse_host_port(self):
        self.assertEqual(monitor.parse_host_port("localhost:11434"), ("localhost", 11434))

        with self.assertRaises(ValueError):
            monitor.parse_host_port("localhost")


class TestLlmFleetMonitorCsv(unittest.TestCase):
    def write_csv(self, rows, fieldnames=None):
        fieldnames = fieldnames or ["sort", "hostname", "description", "endpoint", "ollama", "whisper", "piper", "openai"]
        tmp = tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", delete=False)
        with tmp:
            writer = csv.DictWriter(tmp, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return tmp.name

    def test_read_rows_accepts_valid_single_provider_rows(self):
        path = self.write_csv(
            [
                {
                    "sort": "10",
                    "hostname": "host1",
                    "description": "Ollama host",
                    "endpoint": "127.0.0.1:11434",
                    "ollama": "true",
                    "whisper": "false",
                    "piper": "false",
                    "openai": "false",
                }
            ]
        )

        rows, warnings = monitor.read_rows(path)

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].hostname, "host1")
        self.assertEqual(rows[0].provider, "ollama")
        self.assertEqual(rows[0].sort, 10)

    def test_read_rows_skips_rows_with_multiple_provider_flags(self):
        path = self.write_csv(
            [
                {
                    "sort": "10",
                    "hostname": "bad",
                    "description": "Invalid",
                    "endpoint": "127.0.0.1:1234",
                    "ollama": "true",
                    "whisper": "true",
                    "piper": "false",
                    "openai": "false",
                }
            ]
        )

        rows, warnings = monitor.read_rows(path)

        self.assertEqual(rows, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("expected exactly one provider flag", warnings[0])

    def test_read_rows_skips_bad_endpoint(self):
        path = self.write_csv(
            [
                {
                    "sort": "10",
                    "hostname": "bad",
                    "description": "Invalid",
                    "endpoint": "127.0.0.1",
                    "ollama": "true",
                    "whisper": "false",
                    "piper": "false",
                    "openai": "false",
                }
            ]
        )

        rows, warnings = monitor.read_rows(path)

        self.assertEqual(rows, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("bad endpoint", warnings[0])

    def test_read_rows_parses_sort_field(self):
        path = self.write_csv(
            [
                {
                    "sort": "42",
                    "hostname": "ok",
                    "description": "Has sort",
                    "endpoint": "127.0.0.1:11434",
                    "ollama": "true",
                    "whisper": "false",
                    "piper": "false",
                    "openai": "false",
                }
            ]
        )

        rows, warnings = monitor.read_rows(path)
        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].sort, 42)

    def test_read_rows_skips_row_with_non_integer_sort(self):
        path = self.write_csv(
            [
                {
                    "sort": "banana",
                    "hostname": "bad",
                    "description": "Invalid sort",
                    "endpoint": "127.0.0.1:11434",
                    "ollama": "true",
                    "whisper": "false",
                    "piper": "false",
                    "openai": "false",
                }
            ]
        )

        rows, warnings = monitor.read_rows(path)
        self.assertEqual(rows, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("bad sort value", warnings[0])

    def test_read_rows_rejects_missing_sort_column(self):
        # old header without sort
        path = self.write_csv(
            [
                {
                    "hostname": "ok",
                    "description": "Missing sort column",
                    "endpoint": "127.0.0.1:11434",
                    "ollama": "true",
                    "whisper": "false",
                    "piper": "false",
                    "openai": "false",
                }
            ],
            fieldnames=["hostname", "description", "endpoint", "ollama", "whisper", "piper", "openai"],
        )

        with self.assertRaises(ValueError) as cm:
            monitor.read_rows(path)
        self.assertIn("sort", str(cm.exception))

    def test_read_rows_accepts_openai_provider(self):
        path = self.write_csv(
            [
                {
                    "sort": "10",
                    "hostname": "openai-host",
                    "description": "OpenAI-compatible endpoint",
                    "endpoint": "127.0.0.1:8000",
                    "ollama": "false",
                    "whisper": "false",
                    "piper": "false",
                    "openai": "true",
                }
            ]
        )

        rows, warnings = monitor.read_rows(path)

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].hostname, "openai-host")
        self.assertEqual(rows[0].provider, "openai")

    def test_read_rows_rejects_missing_openai_column(self):
        # CSV without openai column should fail at startup
        path = self.write_csv(
            [
                {
                    "sort": "10",
                    "hostname": "ok",
                    "description": "Missing openai column",
                    "endpoint": "127.0.0.1:11434",
                    "ollama": "false",
                    "whisper": "false",
                    "piper": "true",
                }
            ],
            fieldnames=["sort", "hostname", "description", "endpoint", "ollama", "whisper", "piper"],
        )

        with self.assertRaises(ValueError) as cm:
            monitor.read_rows(path)
        self.assertIn("openai", str(cm.exception))


class TestLlmFleetMonitorRendering(unittest.TestCase):
    def test_render_text_for_unreachable_host(self):
        envelope = {
            "schema_version": 3,
            "probed_at": "now",
            "results": [
                {
                    "sort": 10,
                    "hostname": "host1",
                    "description": "Test host",
                    "endpoint": "127.0.0.1:11434",
                    "provider": "ollama",
                    "reachable": False,
                    "latency_ms": None,
                    "error": {"kind": "timeout", "detail": "timed out"},
                    "ollama": None,
                    "whisper": None,
                    "piper": None,
                    "openai": None,
                }
            ],
        }

        text = monitor.render_text(envelope)

        self.assertIn("host1 — Test host", text)
        self.assertIn("reachable  NO — timeout — timed out", text)

    def test_probe_fleet_uses_read_rows_and_run_probe(self):
        fake_rows = [monitor.Row(10, "h", "d", "127.0.0.1:1", "ollama")]
        fake_envelope = {"schema_version": 3, "probed_at": "x", "results": []}

        with patch.object(monitor, "read_rows", return_value=(fake_rows, [])) as read_rows:
            with patch.object(monitor, "run_probe", return_value=fake_envelope) as run_probe:
                result = monitor.probe_fleet("fleet.csv", timeout=1.5, max_workers=2)

        self.assertEqual(result, fake_envelope)
        read_rows.assert_called_once_with("fleet.csv")
        run_probe.assert_called_once_with(fake_rows, timeout=1.5, max_workers=2)


class TestLlmFleetMonitorSorting(unittest.TestCase):
    def test_run_probe_sorts_results_by_sort_field(self):
        rows = [
            monitor.Row(30, "c", "d3", "127.0.0.1:3", "ollama"),
            monitor.Row(10, "a", "d1", "127.0.0.1:1", "ollama"),
            monitor.Row(20, "b", "d2", "127.0.0.1:2", "ollama"),
        ]

        def fake_build_record(row, timeout):
            return {"sort": row.sort, "hostname": row.hostname}

        with patch.object(monitor, "build_record", side_effect=fake_build_record):
            env = monitor.run_probe(rows, timeout=1.0)

        self.assertEqual([r["sort"] for r in env["results"]], [10, 20, 30])

    def test_run_probe_breaks_sort_ties_by_endpoint(self):
        # Hostnames deliberately sort the OPPOSITE way from endpoints
        # ("a-host" before "z-host" alphabetically, but its endpoint
        # 127.0.0.1:2 is lexically greater than 127.0.0.1:1). If the
        # tiebreak ever regresses to hostname, this assertion order flips
        # and the test fails.
        rows = [
            monitor.Row(10, "a-host", "d", "127.0.0.1:2", "ollama"),
            monitor.Row(10, "z-host", "d", "127.0.0.1:1", "ollama"),
        ]

        def fake_build_record(row, timeout):
            return {"sort": row.sort, "hostname": row.hostname, "endpoint": row.endpoint}

        with patch.object(monitor, "build_record", side_effect=fake_build_record):
            env = monitor.run_probe(rows, timeout=1.0)

        self.assertEqual(
            [r["endpoint"] for r in env["results"]], ["127.0.0.1:1", "127.0.0.1:2"]
        )
        self.assertEqual([r["hostname"] for r in env["results"]], ["z-host", "a-host"])

    def test_run_probe_schema_version_is_3(self):
        rows = [monitor.Row(10, "a", "d", "127.0.0.1:1", "ollama")]

        def fake_build_record(row, timeout):
            return {"sort": row.sort, "hostname": row.hostname}

        with patch.object(monitor, "build_record", side_effect=fake_build_record):
            env = monitor.run_probe(rows, timeout=1.0)

        self.assertEqual(env["schema_version"], 3)


class TestClassifyError(unittest.TestCase):
    # Regression coverage for the refused-vs-protocol misclassification fix:
    # probe_wyoming() used to re-classify anything isinstance(e, ConnectionError)
    # as "protocol", which silently swallowed classify_error()'s correct
    # ConnectionRefusedError -> "refused" result (ConnectionRefusedError is a
    # ConnectionError subclass). The override is gone now; classify_error()
    # is the sole source of truth. These pin its behavior directly.

    def test_connection_refused_is_refused(self):
        kind, _ = monitor.classify_error(ConnectionRefusedError("refused"))
        self.assertEqual(kind, "refused")

    def test_bare_connection_error_is_protocol(self):
        # This is what wyoming_describe() raises when the socket closes
        # before a full describe/info message arrives (see
        # "closed before info" / "closed mid-event" in wyoming_describe).
        kind, _ = monitor.classify_error(ConnectionError("closed before info"))
        self.assertEqual(kind, "protocol")

    def test_connection_reset_is_protocol(self):
        # ConnectionResetError/BrokenPipeError/ConnectionAbortedError are
        # ConnectionError subclasses that aren't ConnectionRefusedError.
        # Previously unclassified ("other"); now fall in with the bare
        # ConnectionError case above as a side effect of the fix.
        kind, _ = monitor.classify_error(ConnectionResetError("reset"))
        self.assertEqual(kind, "protocol")

    def test_timeout_and_dns_unaffected(self):
        self.assertEqual(monitor.classify_error(socket.timeout("timed out"))[0], "timeout")
        self.assertEqual(monitor.classify_error(socket.gaierror("no such host"))[0], "dns")


class TestProbeWyoming(unittest.TestCase):
    # probe_wyoming() itself, not just classify_error() — proves the local
    # override is actually gone and errors pass through unmodified.

    def test_connection_refused_reports_refused(self):
        with patch.object(monitor, "wyoming_describe", side_effect=ConnectionRefusedError("refused")):
            ok, latency, data, err = monitor.probe_wyoming("127.0.0.1", 10300, 1.0, "whisper")

        self.assertFalse(ok)
        self.assertIsNone(latency)
        self.assertIsNone(data)
        self.assertEqual(err["kind"], "refused")

    def test_dropped_handshake_reports_protocol(self):
        with patch.object(monitor, "wyoming_describe", side_effect=ConnectionError("closed before info")):
            ok, latency, data, err = monitor.probe_wyoming("127.0.0.1", 10300, 1.0, "whisper")

        self.assertFalse(ok)
        self.assertEqual(err["kind"], "protocol")

    def test_no_local_override_delegates_entirely_to_classify_error(self):
        # The bug itself, reproduced directly: before the fix, every one of
        # these except timeout/dns collapsed to "protocol" regardless of
        # what classify_error() actually said.
        cases = [
            (ConnectionRefusedError("x"), "refused"),
            (socket.timeout("x"), "timeout"),
            (socket.gaierror("x"), "dns"),
            (ConnectionError("x"), "protocol"),
        ]
        for exc, expected_kind in cases:
            with self.subTest(exc=type(exc).__name__):
                with patch.object(monitor, "wyoming_describe", side_effect=exc):
                    ok, _latency, _data, err = monitor.probe_wyoming("127.0.0.1", 10300, 1.0, "piper")
                self.assertFalse(ok)
                self.assertEqual(err["kind"], expected_kind)


if __name__ == "__main__":
    unittest.main()