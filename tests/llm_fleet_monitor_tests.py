# tests/test_llm_fleet_monitor.py
from __future__ import annotations

import csv
import importlib.util
import json
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
        fieldnames = fieldnames or ["hostname", "description", "endpoint", "ollama", "whisper", "piper"]
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
                    "hostname": "host1",
                    "description": "Ollama host",
                    "endpoint": "127.0.0.1:11434",
                    "ollama": "true",
                    "whisper": "false",
                    "piper": "false",
                }
            ]
        )

        rows, warnings = monitor.read_rows(path)

        self.assertEqual(warnings, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].hostname, "host1")
        self.assertEqual(rows[0].provider, "ollama")

    def test_read_rows_skips_rows_with_multiple_provider_flags(self):
        path = self.write_csv(
            [
                {
                    "hostname": "bad",
                    "description": "Invalid",
                    "endpoint": "127.0.0.1:1234",
                    "ollama": "true",
                    "whisper": "true",
                    "piper": "false",
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
                    "hostname": "bad",
                    "description": "Invalid",
                    "endpoint": "127.0.0.1",
                    "ollama": "true",
                    "whisper": "false",
                    "piper": "false",
                }
            ]
        )

        rows, warnings = monitor.read_rows(path)

        self.assertEqual(rows, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("bad endpoint", warnings[0])


class TestLlmFleetMonitorRendering(unittest.TestCase):
    def test_render_text_for_unreachable_host(self):
        envelope = {
            "schema_version": 1,
            "probed_at": "now",
            "results": [
                {
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
                }
            ],
        }

        text = monitor.render_text(envelope)

        self.assertIn("host1 — Test host", text)
        self.assertIn("reachable  NO — timeout — timed out", text)

    def test_probe_fleet_uses_read_rows_and_run_probe(self):
        fake_rows = [monitor.Row("h", "d", "127.0.0.1:1", "ollama")]
        fake_envelope = {"schema_version": 1, "probed_at": "x", "results": []}

        with patch.object(monitor, "read_rows", return_value=(fake_rows, [])) as read_rows:
            with patch.object(monitor, "run_probe", return_value=fake_envelope) as run_probe:
                result = monitor.probe_fleet("fleet.csv", timeout=1.5, max_workers=2)

        self.assertEqual(result, fake_envelope)
        read_rows.assert_called_once_with("fleet.csv")
        run_probe.assert_called_once_with(fake_rows, timeout=1.5, max_workers=2)


if __name__ == "__main__":
    unittest.main()
