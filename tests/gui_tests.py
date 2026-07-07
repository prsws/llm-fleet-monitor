# tests/test_gui.py
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "gui.py"


def load_gui_module():
    spec = importlib.util.spec_from_file_location("gui", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gui = load_gui_module()


class TestGuiCache(unittest.TestCase):
    def test_set_and_get_envelope_returns_copy(self):
        env = {
            "schema_version": 2,
            "probed_at": "now",
            "results": [{"hostname": "host1"}],
        }

        gui.set_envelope(env)
        got = gui.get_envelope()
        got["results"][0]["hostname"] = "changed"

        self.assertEqual(gui.get_envelope()["results"][0]["hostname"], "host1")

    def test_envelope_ptag_is_stable_for_same_results(self):
        env1 = {"schema_version": 2, "probed_at": "a", "results": [{"hostname": "h"}]}
        env2 = {"schema_version": 2, "probed_at": "b", "results": [{"hostname": "h"}]}

        self.assertEqual(gui.envelope_ptag(env1), gui.envelope_ptag(env2))

    def test_envelope_ptag_changes_when_results_change(self):
        env1 = {"results": [{"hostname": "h1"}]}
        env2 = {"results": [{"hostname": "h2"}]}

        self.assertNotEqual(gui.envelope_ptag(env1), gui.envelope_ptag(env2))


class TestGuiHumanizers(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(gui.human_size(None), "?")
        self.assertEqual(gui.human_size(999), "999 B")
        self.assertEqual(gui.human_size(1_500_000), "1.5 MB")
        self.assertEqual(gui.human_size(2_000_000_000), "2.0 GB")

    def test_human_ttl(self):
        self.assertEqual(gui.human_ttl(None), "?")
        self.assertEqual(gui.human_ttl("forever"), "forever")
        self.assertEqual(gui.human_ttl(5), "5s")
        self.assertEqual(gui.human_ttl(65), "1m5s")
        self.assertEqual(gui.human_ttl(3661), "1h1m")
        self.assertEqual(gui.human_ttl("bad"), "?")

    def test_html_escape(self):
        self.assertEqual(
            gui.html_escape("<tag attr='x'>&\""),
            "&lt;tag attr=&#x27;x&#x27;&gt;&amp;&quot;",
        )


class TestGuiRendering(unittest.TestCase):
    def test_render_cards_fragment_empty_state(self):
        html = gui.render_cards_fragment({"schema_version": 2, "probed_at": None, "results": []})

        self.assertIn('id="host-cards"', html)
        self.assertIn("No hosts to display.", html)

    def test_render_cards_fragment_escapes_host_data(self):
        env = {
            "schema_version": 2,
            "probed_at": "now",
            "results": [
                {
                    "hostname": "<host>",
                    "description": "A&B",
                    "provider": "ollama",
                    "endpoint": "127.0.0.1:11434",
                    "reachable": True,
                    "latency_ms": 12,
                    "ollama": {"version": "1.0", "loaded": [], "downloaded": []},
                }
            ],
        }

        html = gui.render_cards_fragment(env)

        self.assertIn("&lt;host&gt;", html)
        self.assertIn("A&amp;B", html)
        self.assertIn("reachable", html)
        self.assertIn("1.0", html)

    def test_render_full_page_contains_fragment_and_polling_route(self):
        html = gui.render_full_page({"schema_version": 2, "probed_at": None, "results": []})

        self.assertIn("LLM Fleet Monitor", html)
        self.assertIn('hx-get="/fragment/hosts"', html)
        self.assertIn("/htmax.js", html)

    def test_render_details_have_stable_ids_with_slugified_hostname(self):
        env = {
            "schema_version": 2,
            "probed_at": "now",
            "results": [
                {
                    "hostname": "GPU Box #1",
                    "description": "",
                    "provider": "ollama",
                    "endpoint": "127.0.0.1:11434",
                    "reachable": True,
                    "latency_ms": 12,
                    "ollama": {
                        "version": "1.0",
                        "loaded": [
                            {"name": "m1", "size": 1, "size_vram": 1, "gpu_fraction": 1.0, "ttl_seconds": 65}
                        ],
                        "downloaded": [{"name": "m1"}],
                    },
                }
            ],
        }

        html = gui.render_cards_fragment(env)
        # slug("GPU Box #1") -> "gpu-box-1"
        self.assertIn('id="ld-gpu-box-1"', html)
        self.assertIn('id="dl-gpu-box-1"', html)


class TestGuiProbeWrapper(unittest.TestCase):
    def test_probe_once_uses_probe_fleet_when_available(self):
        class FakeModule:
            @staticmethod
            def probe_fleet(csv_path, timeout):
                return {"csv_path": csv_path, "timeout": timeout, "results": []}

        result = gui._probe_once_with(FakeModule(), Path("fleet.csv"), timeout=2.5)

        self.assertEqual(result["csv_path"], "fleet.csv")
        self.assertEqual(result["timeout"], 2.5)

    def test_probe_once_falls_back_to_read_rows_and_run_probe(self):
        class FakeModule:
            @staticmethod
            def read_rows(csv_path):
                return ["row"], []

            @staticmethod
            def run_probe(rows, timeout):
                return {"rows": rows, "timeout": timeout}

        result = gui._probe_once_with(FakeModule(), Path("fleet.csv"), timeout=1.0)

        self.assertEqual(result, {"rows": ["row"], "timeout": 1.0})

    def test_main_returns_2_when_csv_missing_without_fixture(self):
        with patch.object(gui, "ThreadingHTTPServer"):
            code = gui.main(["--csv", "./llm-fleet.csv", "--port", "8766"])

        self.assertEqual(code, 2)

    def test_fixture_mode_loads_empty_envelope_when_fixture_missing(self):
        class FakeServer:
            daemon_threads = False

            def __init__(self, *args, **kwargs):
                self.socket = self

            def getsockname(self):
                return ("127.0.0.1", 0)

            def serve_forever(self):
                return None

            def server_close(self):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            missing_fixture = Path(tmp) / "missing.json"
            with patch.object(gui, "FIXTURE_JSON", missing_fixture):
                with patch.object(gui, "ThreadingHTTPServer", FakeServer):
                    code = gui.main(["--fixture", "--port", "0"])

        self.assertEqual(code, 0)
        self.assertEqual(gui.get_envelope()["results"], [])


class TestPtagNormalization(unittest.TestCase):
    def _base_host(self):
        return {
            "hostname": "h",
            "provider": "ollama",
            "endpoint": "e",
            "reachable": True,
            "latency_ms": 5,
            "ollama": {
                "version": "1.0",
                "loaded": [
                    {
                        "name": "m",
                        "size": 1,
                        "size_vram": 1,
                        "gpu_fraction": 1.0,
                        "ttl_seconds": 65,
                        "expires_at": "soon",
                    }
                ],
                "downloaded": [{"name": "m"}],
            },
        }

    def test_same_minute_bucket_same_tag(self):
        h1 = self._base_host()
        h2 = json.loads(json.dumps(h1))
        h2["ollama"]["loaded"][0]["ttl_seconds"] = 70
        env1 = {"results": [h1]}
        env2 = {"results": [h2]}
        self.assertEqual(gui.envelope_ptag(env1), gui.envelope_ptag(env2))

    def test_different_minute_bucket_different_tag(self):
        h1 = self._base_host()
        h2 = json.loads(json.dumps(h1))
        h2["ollama"]["loaded"][0]["ttl_seconds"] = 130
        env1 = {"results": [h1]}
        env2 = {"results": [h2]}
        self.assertNotEqual(gui.envelope_ptag(env1), gui.envelope_ptag(env2))

    def test_latency_ignored_same_tag(self):
        h1 = self._base_host()
        h2 = json.loads(json.dumps(h1))
        h2["latency_ms"] = 40
        env1 = {"results": [h1]}
        env2 = {"results": [h2]}
        self.assertEqual(gui.envelope_ptag(env1), gui.envelope_ptag(env2))

    def test_forever_same_vs_numeric_different(self):
        h1 = self._base_host()
        h2 = json.loads(json.dumps(h1))
        h1["ollama"]["loaded"][0]["ttl_seconds"] = "forever"
        h2["ollama"]["loaded"][0]["ttl_seconds"] = "forever"
        env1 = {"results": [h1]}
        env2 = {"results": [h2]}
        self.assertEqual(gui.envelope_ptag(env1), gui.envelope_ptag(env2))

        h3 = json.loads(json.dumps(h1))
        h3["ollama"]["loaded"][0]["ttl_seconds"] = 60
        self.assertNotEqual(gui.envelope_ptag(env1), gui.envelope_ptag({"results": [h3]}))

    def test_loaded_appearance_changes_tag(self):
        h1 = self._base_host()
        h2 = json.loads(json.dumps(h1))
        # Remove the loaded model entirely
        h2["ollama"]["loaded"] = []
        env1 = {"results": [h1]}
        env2 = {"results": [h2]}
        self.assertNotEqual(gui.envelope_ptag(env1), gui.envelope_ptag(env2))

    def test_ptag_does_not_mutate_input(self):
        h1 = self._base_host()
        env = {"results": [h1]}
        before = json.loads(json.dumps(env))
        _ = gui.envelope_ptag(env)
        self.assertEqual(before, env)


if __name__ == "__main__":
    unittest.main()
