"""Tests for runtime/hud.py, the JSON feed behind the terser-hud desktop band."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime import hud  # noqa: E402


class HudFeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"TERSER_HOME": str(self.home)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _savings(self, rows: list[dict]) -> None:
        with (self.home / "savings.jsonl").open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_lifetime_and_session_split(self) -> None:
        now = time.time()
        self._savings([
            {"ts": now - 1000, "cat": "read_dedup", "raw_tok": 1000, "saved_tok": 900},
            {"ts": now - 10, "cat": "output_compress", "raw_tok": 200, "saved_tok": 80},
        ])
        flags = self.home / "session-flags"
        flags.mkdir()
        (flags / "abc.json").write_text(json.dumps({"started_at": now - 100}), encoding="utf-8")

        transcript = self.home / "abc.jsonl"
        transcript.write_text(json.dumps({
            "type": "assistant", "timestamp": "2026-11-27T10:00:00Z",
            "message": {"model": "claude-opus-5-5", "role": "assistant", "content": [],
                        "usage": {"input_tokens": 10, "output_tokens": 50,
                                  "cache_read_input_tokens": 900, "cache_creation_input_tokens": 90}},
        }) + "\n", encoding="utf-8")

        data = hud.snapshot(str(transcript))
        self.assertEqual(data["lifetime"]["saved_tok"], 980)
        self.assertEqual(data["lifetime"]["by_cat"][0]["cat"], "read_dedup")
        sess = data["session"]
        self.assertEqual(sess["saved_tok"], 80)
        self.assertEqual(sess["model"], "claude-opus-5-5")
        self.assertGreater(sess["cost_usd"], 0)
        self.assertIn("letter", sess["grade"])
        self.assertAlmostEqual(sess["cache_hit"], 900 / 1000, places=3)

    def test_main_prints_one_json_line_without_state(self) -> None:
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            self.assertEqual(hud.main(["--json"]), 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["lifetime"]["saved_tok"], 0)
        self.assertIsNone(data["session"])

    def test_antigravity_transcript_auto_discovery_and_parsing(self) -> None:
        now = time.time()
        self._savings([
            {"ts": now - 5, "cat": "read_outline", "raw_tok": 2000, "saved_tok": 1600},
        ])
        brain = self.home / "brain"
        logs = brain / "conv-1234" / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        tr = logs / "transcript.jsonl"
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-27T19:00:00Z", "content": "<USER_REQUEST>\nFix bug\n</USER_REQUEST>"},
            {
                "step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "2026-11-27T19:00:02Z",
                "model": "gemini-4-neon-medium", "input_tokens": 10000, "cache_read_tokens": 40000, "output_tokens": 500,
                "tool_calls": [{"name": "read_file", "args": {"Path": '"a.py"'}}],
            },
            {"step_index": 2, "type": "GENERIC", "status": "ERROR", "error": "tool call denied by pre-tool hook: [Terser] outline", "content": ""},
        ]
        tr.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

        with mock.patch.dict(os.environ, {"ANTIGRAVITY_BRAIN_DIR": str(brain)}):
            data = hud.snapshot(host="antigravity")
        self.assertEqual(data["host"], "antigravity")
        sess = data["session"]
        self.assertIsNotNone(sess)
        self.assertEqual(sess["session_id"], "conv-1234")
        self.assertEqual(sess["model"], "gemini-4-neon-medium")
        self.assertEqual(sess["ctx_window"], 1_000_000)
        self.assertAlmostEqual(sess["ctx_fill"], 0.05, places=3)
        self.assertAlmostEqual(sess["cache_hit"], 0.8, places=3)
        self.assertEqual(sess["tool_errors"], 0)  # Terser outline hook denial is not a failure
        self.assertEqual(sess["grade"]["letter"], "A")

    def test_mcp_app_protocol_for_antigravity(self) -> None:
        from runtime import index_mcp

        with mock.patch.dict(os.environ, {"TERSER_CLIENT_ID": "antigravity"}):
            init = index_mcp._handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
            self.assertIn("resources", init["result"]["capabilities"])

            tlist = index_mcp._handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = {t["name"]: t for t in tlist["result"]["tools"]}
            self.assertIn("terser_hud", names)
            self.assertIn("terser_hud_snapshot", names)
            self.assertIn("terser_hud_set_config", names)
            self.assertEqual(names["terser_hud"]["_meta"]["ui"]["resourceUri"], "ui://terser/hud.html")
            self.assertEqual(names["terser_hud_snapshot"]["_meta"]["ui"]["visibility"], ["app"])

            rlist = index_mcp._handle_request({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
            self.assertEqual(rlist["result"]["resources"][0]["uri"], "ui://terser/hud.html")
            self.assertEqual(rlist["result"]["resources"][0]["mimeType"], "text/html;profile=mcp-app")

            rread = index_mcp._handle_request({"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "ui://terser/hud.html"}})
            html_text = rread["result"]["contents"][0]["text"]
            self.assertIn("ui/initialize", html_text)
            self.assertIn("Terser", html_text)

            call = index_mcp._handle_request({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "terser_hud", "arguments": {}}})
            self.assertIn("structuredContent", call["result"])
            self.assertIn("lifetime", call["result"]["structuredContent"])
            self.assertTrue(call["result"]["content"][0]["text"].startswith("Terser HUD"))

            upd = index_mcp._handle_request({
                "jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "terser_hud_set_config", "arguments": {"updates": {"shadow": True, "subagent_model_routing": True, "subagent_cheap_model": "flash"}}},
            })
            self.assertTrue(upd["result"]["structuredContent"]["shadow"])
            self.assertTrue(upd["result"]["structuredContent"]["routing"]["enabled"])
            self.assertEqual(upd["result"]["structuredContent"]["routing"]["model"], "flash")

            bad = index_mcp._handle_request({
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "terser_hud_set_config", "arguments": {"updates": {"not_a_real_key": True}}},
            })
            self.assertEqual(bad["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
