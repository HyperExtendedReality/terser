"""Tests for Terser's native replacements of the former token_optimizer vendor code."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime import analytics, code_outline, command_handlers, redaction, shell_safety, smart_crusher  # noqa: E402


class CommandHandlerTests(unittest.TestCase):
    def test_git_status_groups_and_drops_hints(self) -> None:
        raw = "On branch main\nChanges not staged for commit:\n  (use \"git add <file>...\" to update)\n" + "".join(
            f"\tmodified:   src/m{i}.py\n" for i in range(6)
        )
        out = command_handlers.compress("git status", raw)
        self.assertIn("modified (6): src/m0.py", out)
        self.assertNotIn("(use \"git add", out)
        self.assertIn("1 git hint", out)

    def test_failed_non_test_command_untouched(self) -> None:
        raw = "Collecting foo\nERROR: No matching distribution found for foo\n" * 3
        self.assertEqual(command_handlers.compress("pip install foo", raw, returncode=1), raw)

    def test_cargo_test_keeps_failures_only(self) -> None:
        raw = "".join(f"test t{i} ... ok\n" for i in range(30)) + "test bad ... FAILED\n---- bad stdout ----\npanicked\ntest result: FAILED. 30 passed; 1 failed\n"
        out = command_handlers.compress("cargo test", raw, returncode=101)
        self.assertIn("test bad ... FAILED", out)
        self.assertIn("panicked", out)
        self.assertNotIn("test t3 ... ok", out)

    def test_go_test_summary(self) -> None:
        raw = "".join(f"=== RUN   T{i}\n--- PASS: T{i} (0.00s)\n" for i in range(10)) + "PASS\nok  \tpkg/a\t0.1s\n"
        out = command_handlers.compress("go test ./...", raw)
        self.assertIn("ok: 1 package(s) passed", out)

    def test_unknown_command_passthrough(self) -> None:
        self.assertEqual(command_handlers.compress("mytool run", "x\n" * 50), "x\n" * 50)


class OutlineTests(unittest.TestCase):
    def test_csharp_nesting_and_spans(self) -> None:
        src = "using System;\nnamespace G {\n  public class P {\n    public void A() {\n    }\n    private int B(int x) {\n      return x;\n    }\n  }\n}\n"
        out = code_outline.outline(src, "P.cs") or ""
        self.assertIn("L2-10: namespace G", out)
        self.assertIn("L6-8: private int B(int x)", out)

    def test_rust_does_not_match_identifiers_starting_with_keywords(self) -> None:
        src = "fn a() {\n}\nfn b() {\n}\nfn c() {\n  let mode_tag = 1;\n}\n"
        out = code_outline.outline(src, "x.rs") or ""
        self.assertNotIn("mode_tag", out)

    def test_gdscript(self) -> None:
        src = "extends Node\nsignal hit\nfunc _ready():\n\tpass\nfunc move(d):\n\tpass\n"
        out = code_outline.outline(src, "p.gd") or ""
        self.assertIn("func _ready()", out)
        self.assertIn("signal hit", out)


class SafetyAndRedactionTests(unittest.TestCase):
    def test_whitelist(self) -> None:
        self.assertTrue(shell_safety.is_whitelisted("git status"))
        self.assertTrue(shell_safety.is_whitelisted("python -m pytest -q"))
        for bad in ("git status; rm -rf x", "git commit -m x", "npm test -- --watch", "tail -f a.log", "find . -delete", "echo $(id)"):
            self.assertFalse(shell_safety.is_whitelisted(bad), bad)

    def test_readonly_subset(self) -> None:
        self.assertTrue(shell_safety.is_auto_approvable("git diff --stat"))
        self.assertFalse(shell_safety.is_auto_approvable("cargo test"))
        self.assertFalse(shell_safety.is_auto_approvable("git branch -D main"))

    def test_redaction(self) -> None:
        out = redaction.redact("API_KEY=abcd1234 Authorization: Bearer zzz https://u:pw@h.io ghp_" + "a" * 36)
        self.assertNotIn("abcd1234", out)
        self.assertNotIn("zzz", out)
        self.assertNotIn(":pw@", out)
        self.assertNotIn("ghp_aaaa", out)
        self.assertIn("API_KEY=", out)
        self.assertNotIn("hunter2", redaction.redact("mysql -phunter2", command=True))


class CrusherTests(unittest.TestCase):
    def test_sequence_columns_round_trip(self) -> None:
        items = [{"id": i, "name": f"n{i}", "zone": "eu", "v": 10 + 2 * i} for i in range(8)]
        enc = smart_crusher._factor_and_columnize(items)
        self.assertEqual(enc["_seq"], {"id": {"start": 0, "step": 1}, "v": {"start": 10, "step": 2}})
        rebuilt = []
        for i, row in enumerate(enc["rows"]):
            item = dict(enc.get("_const", {}))
            item.update(zip(enc["cols"], row))
            for k, sq in enc["_seq"].items():
                item[k] = sq["start"] + i * sq["step"]
            rebuilt.append(item)
        self.assertEqual([dict(sorted(x.items())) for x in rebuilt], [dict(sorted(x.items())) for x in items])


class AnalyticsTests(unittest.TestCase):
    def _transcript(self, d: Path) -> Path:
        p = d / "s.jsonl"
        recs = [
            {"type": "user", "sessionId": "s1", "cwd": str(d), "timestamp": "2026-11-27T10:00:00Z", "message": {"content": "fix the bug"}},
            {"type": "assistant", "timestamp": "2026-11-27T10:00:05Z", "message": {"model": "m", "usage": {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100, "output_tokens": 50},
             "content": [{"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "x.py"}},
                         {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "x.py"}},
                         {"type": "tool_use", "id": "c", "name": "Edit", "input": {"file_path": "x.py"}}]}},
            {"type": "user", "timestamp": "2026-11-27T10:00:06Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "c", "is_error": True, "content": "old_string not found"}]}},
        ]
        p.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
        return p

    def test_parse_grade_and_capture(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            st = analytics.parse_claude_transcript(self._transcript(d))
            self.assertEqual(st.turns, 1)
            self.assertEqual(st.repeated_reads, 1)
            self.assertEqual(st.tool_errors, 1)
            letter, score, reasons = analytics.quality_grade(st)
            self.assertLess(score, 100)
            self.assertTrue(reasons)
            env = dict(os.environ, TERSER_HOME=str(d / "home"))
            path = analytics.compact_capture({"transcript_path": str(d / "s.jsonl"), "cwd": str(d)}, env=env)
            self.assertIsNotNone(path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("Edited files: x.py", text)
            self.assertIn("fix the bug", text)
            self.assertIn("x.py", analytics.compact_instructions({"transcript_path": str(d / "s.jsonl")}))

    def test_cost_uses_cached_rates(self) -> None:
        price = dict(analytics._PRICING_DEFAULTS)
        # write once at 1.25x + 3 cached re-reads at 0.1x, far below 4 full-price sends
        self.assertAlmostEqual(analytics.saved_cost(1_000_000, price), 3.0 * 1.55)


class ScopedReadTests(unittest.TestCase):
    def test_scoped_read_is_not_windowed_below_cap(self) -> None:
        from runtime import output_compressor as oc

        body = "".join(f"line {i} of the requested section with some words\n" for i in range(600))
        out, _ = oc.compress_output(body, command="sed -n '1,600p' README.md", returncode=0)
        self.assertNotIn("routine middle lines omitted", out)
        self.assertIn("line 300 of", out)
        self.assertTrue(oc.is_scoped_read('tail -n 50 build.log'))
        self.assertFalse(oc.is_scoped_read("cargo build"))


if __name__ == "__main__":
    unittest.main()
