# tests/test_gui.py
from __future__ import annotations

import importlib.util
import json
import re
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
            "schema_version": 3,
            "probed_at": "now",
            "results": [{"hostname": "host1"}],
        }

        gui.set_envelope(env)
        got = gui.get_envelope()
        got["results"][0]["hostname"] = "changed"

        self.assertEqual(gui.get_envelope()["results"][0]["hostname"], "host1")

    def test_envelope_ptag_is_stable_for_same_results(self):
        env1 = {"schema_version": 3, "probed_at": "a", "results": [{"hostname": "h"}]}
        env2 = {"schema_version": 3, "probed_at": "b", "results": [{"hostname": "h"}]}

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
        html = gui.render_cards_fragment({"schema_version": 3, "probed_at": None, "results": []})

        self.assertIn('id="host-cards"', html)
        self.assertIn("No hosts to display.", html)

    def test_render_cards_fragment_escapes_host_data(self):
        env = {
            "schema_version": 3,
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
        # Reachability state is asserted via the stable id/data-reachable
        # marker, not display copy — the visible wording (was "Up?", now
        # mid-migration to an emoji treatment) is UI copy in flux and isn't
        # what this test should be pinned to. id is keyed on (endpoint,
        # provider), not the (deliberately unusual/untrusted) "<host>"
        # hostname in this fixture.
        self.assertIn('id="status-127-0-0-1-11434-ollama"', html)
        self.assertIn('data-reachable="true"', html)
        self.assertIn("1.0", html)

    def test_render_cards_fragment_unreachable_state(self):
        env = {
            "schema_version": 3,
            "probed_at": "now",
            "results": [
                {
                    "hostname": "gpu-box",
                    "description": "",
                    "provider": "ollama",
                    "endpoint": "127.0.0.1:11434",
                    "reachable": False,
                    "latency_ms": None,
                    "error": {"kind": "timeout", "detail": "timed out"},
                }
            ],
        }

        html = gui.render_cards_fragment(env)

        # id keyed on (endpoint, provider), not the "gpu-box" hostname.
        self.assertIn('id="status-127-0-0-1-11434-ollama"', html)
        self.assertIn('data-reachable="false"', html)
        self.assertIn('data-error-kind="timeout"', html)

    def test_render_full_page_contains_fragment_and_polling_route(self):
        html = gui.render_full_page({"schema_version": 3, "probed_at": None, "results": []})

        self.assertIn("LLM Fleet Monitor", html)
        self.assertIn('hx-get="/fragment/hosts"', html)
        self.assertIn("/htmax.js", html)

    def test_render_details_have_stable_ids_from_endpoint(self):
        # hostname is deliberately weird/free-text here ("GPU Box #1") to
        # prove it plays no part in id generation any more — only
        # (endpoint, provider) does.
        env = {
            "schema_version": 3,
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
        self.assertIn('id="ld-127-0-0-1-11434-ollama"', html)
        self.assertIn('id="dl-127-0-0-1-11434-ollama"', html)
        self.assertIn('data-count="1"', html)
        # Old hostname-derived id format must not reappear.
        self.assertNotIn('id="ld-gpu-box', html)
        self.assertNotIn('id="dl-gpu-box', html)

    def test_render_cards_fragment_ollama_empty_loaded_and_downloaded_share_ids(self):
        # Same gap as openai's empty-inventory branch had: the empty-state
        # "nothing running" / "0 downloaded" branches must still carry
        # ld-{slug}/dl-{slug} ids and data-count="0", not just the non-empty
        # <details> branches.
        env = {
            "schema_version": 3,
            "probed_at": "now",
            "results": [
                {
                    "hostname": "gpu-box",
                    "description": "",
                    "provider": "ollama",
                    "endpoint": "127.0.0.1:11434",
                    "reachable": True,
                    "latency_ms": 12,
                    "ollama": {"version": "1.0", "loaded": [], "downloaded": []},
                }
            ],
        }

        html = gui.render_cards_fragment(env)

        self.assertIn('id="ld-127-0-0-1-11434-ollama"', html)
        self.assertIn('id="dl-127-0-0-1-11434-ollama"', html)
        # Both blocks are empty, so both data-count="0" markers should appear.
        self.assertEqual(html.count('data-count="0"'), 2)

    def test_render_cards_fragment_openai_provider(self):
        env = {
            "schema_version": 3,
            "probed_at": "now",
            "results": [
                {
                    "hostname": "llamabox",
                    "description": "llama.cpp on the NUC",
                    "provider": "openai",
                    "endpoint": "192.168.1.40:8080",
                    "reachable": True,
                    "latency_ms": 25,
                    "openai": {
                        "server": "llama.cpp",
                        "models": [
                            {"id": "qwen2.5-coder-14b", "owned_by": None},
                            {"id": "gemma-2-9b", "owned_by": "google"},
                        ],
                    },
                }
            ],
        }

        html = gui.render_cards_fragment(env)

        self.assertIn("llamabox", html)
        self.assertIn("llama.cpp", html)
        # Identify/verify the models block via id + data-count, not display
        # copy — "Downloaded models" (non-empty) and "Available models"
        # (empty) label the same concept differently, so tests shouldn't
        # depend on either wording to know the block is there and populated.
        self.assertIn('id="oa-192-168-1-40-8080-openai"', html)
        self.assertIn('data-count="2"', html)
        self.assertIn("qwen2.5-coder-14b", html)
        self.assertIn("gemma-2-9b", html)
        self.assertIn("google", html)

    def test_render_cards_fragment_openai_provider_empty_models_shares_id(self):
        # The empty-inventory branch must expose the same id/data-count
        # contract as the populated branch above, even though its display
        # copy ("Available models") differs from the populated branch's
        # ("Downloaded models").
        env = {
            "schema_version": 3,
            "probed_at": "now",
            "results": [
                {
                    "hostname": "llamabox",
                    "description": "llama.cpp on the NUC",
                    "provider": "openai",
                    "endpoint": "192.168.1.40:8080",
                    "reachable": True,
                    "latency_ms": 25,
                    "openai": {"server": "llama.cpp", "models": []},
                }
            ],
        }

        html = gui.render_cards_fragment(env)

        self.assertIn('id="oa-192-168-1-40-8080-openai"', html)
        self.assertIn('data-count="0"', html)

    def test_render_cards_fragment_same_hostname_same_provider_no_id_collision(self):
        # Reproduces the real bug (two macstudio rows in production: native
        # llama.cpp on :8000 and Ollama's OpenAI-compat shim on :11434, same
        # hostname, same provider). hostname is user-configurable free text
        # with no uniqueness guarantee, so it must play no part in id
        # generation — only (endpoint, provider) does.
        env = {
            "schema_version": 3,
            "probed_at": "now",
            "results": [
                {
                    "hostname": "macstudio",
                    "description": "native llama.cpp",
                    "provider": "openai",
                    "endpoint": "192.168.10.5:8000",
                    "reachable": True,
                    "latency_ms": 10,
                    "openai": {"server": "llama.cpp", "models": [{"id": "m1", "owned_by": None}]},
                },
                {
                    "hostname": "macstudio",
                    "description": "OpenAI-compatible via Ollama",
                    "provider": "openai",
                    "endpoint": "192.168.10.5:11434",
                    "reachable": True,
                    "latency_ms": 12,
                    "openai": {"server": "ollama", "models": [{"id": "m2", "owned_by": None}]},
                },
            ],
        }

        html = gui.render_cards_fragment(env)

        self.assertIn('id="oa-192-168-10-5-8000-openai"', html)
        self.assertIn('id="oa-192-168-10-5-11434-openai"', html)
        self.assertIn('id="status-192-168-10-5-8000-openai"', html)
        self.assertIn('id="status-192-168-10-5-11434-openai"', html)

        # General invariant: no id attribute value repeats anywhere in the
        # fragment, for any of the two rows' ld-/dl-/oa-/status- ids.
        ids = re.findall(r'id="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)), f"duplicate DOM ids: {ids}")

    def test_render_cards_fragment_same_endpoint_different_provider_no_id_collision(self):
        # The OTHER real case found by cross-checking against
        # example.llm-fleet.csv: Ollama serves its native API and an
        # OpenAI-compatible /v1/models shim on the SAME port, so a fleet can
        # legitimately probe the identical host:port as two different
        # providers (rows 20 + 40 in example.llm-fleet.csv both hit
        # 192.168.10.5:11434 — one as ollama, one as openai). endpoint alone
        # would collide here; only status-{slug} is at risk since ld-/dl-/oa-
        # already carry a provider-specific prefix, but provider is folded
        # into the shared slug for all four rather than special-casing one.
        env = {
            "schema_version": 3,
            "probed_at": "now",
            "results": [
                {
                    "hostname": "macstudio",
                    "description": "Ollama native API",
                    "provider": "ollama",
                    "endpoint": "192.168.10.5:11434",
                    "reachable": True,
                    "latency_ms": 10,
                    "ollama": {"version": "0.31.1", "loaded": [], "downloaded": []},
                },
                {
                    "hostname": "macstudio",
                    "description": "OpenAI-compatible shim via Ollama, same port",
                    "provider": "openai",
                    "endpoint": "192.168.10.5:11434",
                    "reachable": True,
                    "latency_ms": 11,
                    "openai": {"server": "ollama", "models": []},
                },
            ],
        }

        html = gui.render_cards_fragment(env)

        self.assertIn('id="status-192-168-10-5-11434-ollama"', html)
        self.assertIn('id="status-192-168-10-5-11434-openai"', html)

        ids = re.findall(r'id="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)), f"duplicate DOM ids: {ids}")

    def test_render_cards_fragment_whisper_and_piper_summary(self):
        # Whisper: 1 installed model across 99 langs; Piper: 3 voices across 2 langs.
        # RECONSTRUCTED — the uploaded file had this method's body replaced by an
        # unrelated, mis-indented fragment (see test_probe_once_uses_probe_fleet_when_available
        # below for where that fragment actually belongs). Body rebuilt from gui.py's
        # actual whisper/piper rendering strings ("{n} models across ~{k} langs",
        # "{n} voices across {m} langs") rather than recovered — please double-check.
        env = {
            "schema_version": 3,
            "probed_at": "now",
            "results": [
                {
                    "hostname": "voice-stt",
                    "description": "Whisper STT",
                    "provider": "whisper",
                    "endpoint": "127.0.0.1:10300",
                    "reachable": True,
                    "latency_ms": 5,
                    "whisper": {
                        "program": "mlx-whisper",
                        "version": "1.4.0",
                        "models": [
                            {
                                "name": "whisper-large-v3-turbo",
                                "languages": [f"lang{i}" for i in range(99)],
                            }
                        ],
                    },
                },
                {
                    "hostname": "voice-tts",
                    "description": "Piper TTS",
                    "provider": "piper",
                    "endpoint": "127.0.0.1:10200",
                    "reachable": True,
                    "latency_ms": 8,
                    "piper": {
                        "program": "piper",
                        "version": "2.2.2",
                        "voices": [
                            {"name": "en_US-amy-low", "languages": ["en_US"]},
                            {"name": "en_US-amy-medium", "languages": ["en_US"]},
                            {"name": "es_ES-carlfm-x_low", "languages": ["es_ES"]},
                        ],
                    },
                },
            ],
        }

        html = gui.render_cards_fragment(env)

        self.assertIn("1 models across ~99 langs", html)
        self.assertIn("3 voices across 2 langs", html)

    def test_probe_once_uses_probe_fleet_when_available(self):
        # RECONSTRUCTED — this is the fragment that was incorrectly spliced into
        # test_render_cards_fragment_whisper_and_piper_summary above (missing its
        # enclosing `class FakeModule:` / `@staticmethod`, which is why it threw
        # IndentationError). Logic and assertions are unchanged from the upload,
        # just given back its own method and proper structure.
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
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.csv"
            with patch.object(gui, "ThreadingHTTPServer"):
                code = gui.main(["--csv", str(missing), "--port", "8766"])

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

    def test_installed_flag_changes_tag(self):
        # Two envelopes identical except one voice's installed flips -> tags differ
        h1 = {
            "hostname": "h",
            "provider": "piper",
            "endpoint": "e",
            "reachable": True,
            "piper": {"program": "p", "version": "1", "voices": [{"name": "v1", "languages": ["en"], "installed": True}]},
        }
        h2 = json.loads(json.dumps(h1))
        h2["piper"]["voices"][0]["installed"] = False
        env1 = {"results": [h1]}
        env2 = {"results": [h2]}
        self.assertNotEqual(gui.envelope_ptag(env1), gui.envelope_ptag(env2))


if __name__ == "__main__":
    unittest.main()