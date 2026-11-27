"""Regression tests for Terser quality fixes, sibling-repo ports, and Antigravity 2.0 routing/HUD."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[1])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from runtime import (
    analytics,
    antigravity_proto,
    config,
    context_sandbox,
    hud,
    index_mcp,
    output_compressor,
    smart_crusher,
    subagent_verify,
    unified_hook,
)


def _encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_varint(fnum: int, val: int) -> bytes:
    return _encode_varint((fnum << 3) | 0) + _encode_varint(val)


def _pb_bytes(fnum: int, data: bytes) -> bytes:
    return _encode_varint((fnum << 3) | 2) + _encode_varint(len(data)) + data


class TestQualityAndAntigravityImprovements(unittest.TestCase):
    def test_html_code_not_stripped_outside_web_commands(self) -> None:
        jsx = (
            "export function App() {\n"
            "  return (\n"
            "    <div className='app'>\n"
            "      <nav><a href='/home'>Home</a></nav>\n"
            "      <script>console.log('keep me');</script>\n"
            "      <svg><path d='M0 0 L10 10'/></svg>\n"
            "      <footer>" + ("x" * 400) + "</footer>\n"
            "    </div>\n"
            "  );\n"
            "}\n"
        )
        out, _ = output_compressor.compress_output(jsx, command="git diff", enhanced=True)
        self.assertIn("<script>console.log('keep me');</script>", out)
        self.assertIn("<svg>", out)

    def test_error_pinned_window_preserves_traceback_context(self) -> None:
        lines = [f"routine line {i}" for i in range(120)]
        lines[60] = '  File "app.py", line 42, in run'
        lines[61] = "    result = compute_value(x)"
        lines[62] = "ValueError: bad input"
        raw = "\n".join(lines)
        windowed = output_compressor._error_pinned_window(raw, max_chars=2500, archive_id=None)
        self.assertIn('File "app.py", line 42', windowed)
        self.assertIn("result = compute_value(x)", windowed)
        self.assertIn("ValueError: bad input", windowed)

    def test_smart_crusher_preserves_empty_sibling_keys(self) -> None:
        payload = {
            "errors": [],
            "next_cursor": None,
            "items": [
                {"id": i, "name": f"service_endpoint_{i}", "status": "healthy", "region": "us-east-1", "version": "1.2.3"}
                for i in range(25)
            ],
        }
        crushed = smart_crusher.crush_json(json.dumps(payload), allow_sample=False)
        self.assertIsNotNone(crushed)
        parsed = json.loads(crushed or "{}")
        self.assertIn("errors", parsed)
        self.assertEqual(parsed["errors"], [])
        self.assertIn("next_cursor", parsed)
        self.assertIsNone(parsed["next_cursor"])

    def test_antigravity_proto_decode_and_fail_closed(self) -> None:
        ctx_win = _pb_varint(1, 750_000) + _pb_varint(4, 1_000_000)
        chat_start = _pb_bytes(10, ctx_win)
        usage = _pb_varint(2, 1200) + _pb_varint(3, 400) + _pb_varint(5, 8000) + _pb_varint(9, 250)
        gen = _pb_bytes(4, usage) + _pb_bytes(9, chat_start) + _pb_bytes(21, b"Gemini 4 Neon")
        outer = _pb_bytes(1, gen)
        rec = antigravity_proto.decode_gen_metadata(outer)
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertEqual(rec["model"], "Gemini 4 Neon")
        self.assertEqual(rec["thinking_tokens"], 250)
        self.assertAlmostEqual(rec["context_fill"], 0.75, places=4)
        self.assertIsNone(antigravity_proto.decode_gen_metadata(b"\xff\xff\xff\xff"))

    def test_model_aware_pricing_and_full_transcript_preference(self) -> None:
        p_flash = analytics.pricing({}, model="gemini-3-flash")
        self.assertEqual(p_flash["cache_write_mult"], 0.0)
        self.assertLess(p_flash["price_input_per_mtok"], 1.0)
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / "conv1" / ".system_generated" / "logs"
            logs.mkdir(parents=True)
            (logs / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
            (logs / "transcript_full.jsonl").write_text("{}\n", encoding="utf-8")
            found = analytics.latest_antigravity_transcript({"ANTIGRAVITY_BRAIN_DIR": td})
            self.assertIsNotNone(found)
            assert found is not None
            self.assertEqual(found.name, "transcript_full.jsonl")

    def test_sqlite_heal_healthy_db(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "test.db"
            conn = sqlite3.connect(str(db))
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
            conn.close()
            res = context_sandbox.heal_sqlite_db(db)
            self.assertEqual(res["status"], "ok")

    def test_subagent_verify_multi_citation_and_effort_contract(self) -> None:
        prompt = subagent_verify.routed_prompt("Find where load_config is defined", effort="low")
        self.assertIn(subagent_verify.MARKER, prompt)
        self.assertIn("Effort=low", prompt)
        with tempfile.TemporaryDirectory() as td:
            f1 = Path(td) / "a.py"
            f2 = Path(td) / "b.py"
            f1.write_text("def alpha_fn():\n    return 1\n", encoding="utf-8")
            f2.write_text("def beta_fn():\n    return 2\n", encoding="utf-8")
            reply = "See `alpha_fn` in a.py:1 and `beta_fn` in b.py:1 (flag `--fast`)."
            res = subagent_verify.verify(reply, td)
            self.assertTrue(res["ok"], res)

    def test_antigravity_hud_routing_and_string_toggles(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            old_home = os.environ.get("TERSER_HOME")
            os.environ["TERSER_HOME"] = td
            try:
                res = index_mcp._handle_hud_tool(
                    "terser_hud_set_config",
                    {
                        "updates": {
                            "subagent_model_routing": True,
                            "subagent_cheap_model_antigravity": "flash_lite",
                            "subagent_cheap_effort_antigravity": "medium",
                            "subagent_verify": True,
                            "toolprune": "off",
                            "code_discipline": "minimal",
                        }
                    },
                )
                snap = res["structuredContent"]
                self.assertEqual(snap["routing"]["model"], "flash_lite")
                self.assertEqual(snap["routing"]["effort"], "medium")
                self.assertEqual(snap["routing"]["model_key"], "subagent_cheap_model_antigravity")
                self.assertEqual(snap["routing"]["effort_key"], "subagent_cheap_effort_antigravity")
                self.assertTrue(snap["routing"]["enabled"])
                self.assertEqual(snap["features"]["toolprune"], "off")
                self.assertEqual(snap["features"]["code_discipline"], "minimal")
            finally:
                if old_home is None:
                    os.environ.pop("TERSER_HOME", None)
                else:
                    os.environ["TERSER_HOME"] = old_home

    def test_antigravity_pre_tool_use_routes_and_pre_invocation_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {"TERSER_HOME": td}
            cfg = dict(config.DEFAULT_CONFIG)
            cfg["subagent_model_routing"] = True
            cfg["subagent_cheap_model_antigravity"] = "flash_lite"
            cfg["subagent_cheap_effort_antigravity"] = "low"
            cfg["subagent_verify"] = True

            pre = unified_hook.handle_pre_tool_use(
                "antigravity",
                {
                    "conversationId": "conv-verify-test",
                    "cwd": td,
                    "toolCall": {
                        "name": "invoke_subagent",
                        "args": {
                            "TypeName": "research",
                            "Prompt": "Find where foo is defined",
                        },
                    },
                },
                cfg,
                env,
            )
            self.assertIsNotNone(pre)
            assert pre is not None
            self.assertEqual(pre["overwrite"]["Model"], "flash_lite")
            self.assertIn(subagent_verify.MARKER, pre["overwrite"]["Prompt"])
            self.assertIn("Effort=low", pre["overwrite"]["Prompt"])

            # Simulate subagent reply landing in transcript with a bad citation
            tp = Path(td) / "transcript.jsonl"
            bad_msg = {
                "type": "SYSTEM_MESSAGE",
                "content": "[Message] sender=sub1 content=Found `missing_fn` at nonexistent.py:99",
            }
            tp.write_text(json.dumps(bad_msg) + "\n", encoding="utf-8")

            inv = unified_hook.handle_pre_invocation(
                "antigravity",
                {
                    "conversationId": "conv-verify-test",
                    "cwd": td,
                    "invocationNum": 2,
                    "transcriptPath": str(tp),
                },
                cfg,
                env,
            )
            steps = inv.get("injectSteps") or []
            joined = "\n".join(str(s) for s in steps)
            self.assertIn("[Terser Verify]", joined)
            self.assertIn("nonexistent.py", joined)
            st = subagent_verify.stats(env)
            self.assertEqual(st["checked"], 1)
            self.assertEqual(st["failed"], 1)


if __name__ == "__main__":
    unittest.main()
