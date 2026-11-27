"""Tests for `terse discover --replay`, regret signals, and the configurable value model."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime import analytics, cli  # noqa: E402

def _ts(sec: int) -> str:
    m, s = divmod(sec, 60)
    return f"2026-11-27T10:{m:02d}:{s:02d}Z"


def _big_python_source() -> str:
    parts = []
    for i in range(70):
        parts.append(f"def handler_number_{i}(request, response, context=None):")
        parts.append(f"    \"\"\"Handle request type {i} and write a response for the caller.\"\"\"")
        parts.append(f"    value = compute_something_expensive(request.payload, factor={i})")
        parts.append("    return response.write(value)")
        parts.append("")
    return "\n".join(parts) + "\n"


class _Transcript:
    """Builds a minimal Claude Code transcript with paired tool_use / tool_result records."""

    def __init__(self) -> None:
        self.lines: list[dict] = []
        self.n = 0

    def call(self, sec: int, name: str, inp: dict, result, *, is_error: bool = False) -> None:
        self.n += 1
        tid = f"toolu_{self.n}"
        self.lines.append({
            "type": "assistant", "timestamp": _ts(sec), "sessionId": "s1", "cwd": "/work/proj",
            "message": {
                "model": "claude-test",
                "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100},
                "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
            },
        })
        self.lines.append({
            "type": "user", "timestamp": _ts(sec + 1), "sessionId": "s1", "cwd": "/work/proj",
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": is_error}]},
        })

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in self.lines) + "\n", encoding="utf-8")
        return path


def _fixture(root: Path) -> Path:
    t = _Transcript()
    noisy = "\n".join(["Downloading dependency package ... done"] * 400 + ["Build finished OK"])
    t.call(0, "Bash", {"command": "npm install"}, noisy)
    # A Terser-compressed output, then the byte-identical command again 30 s later: a regret signal.
    terser_out = "line one\nline two\n[Terser] output truncated; full output archived. Run `terse expand ab12cd34` for the rest."
    t.call(10, "Bash", {"command": "pytest -q"}, terser_out)
    t.call(40, "Bash", {"command": "pytest -q"}, "3 passed")
    # Same command 6 minutes apart is not a quick re-run.
    t.call(50, "Bash", {"command": "git status"}, "clean")
    t.call(410, "Bash", {"command": "git status"}, "clean")
    # Archive expands: terser-index `order` action and the CLI; a grep that mentions it is not one.
    t.call(420, "mcp__plugin_terser_terser-index__order", {"action": "expand_sandbox", "id": "ab12cd34"}, "full text")
    t.call(430, "Bash", {"command": "terse expand ab12cd34 1 40"}, "full text lines")
    t.call(440, "Bash", {"command": 'grep -rn "terse expand" README.md'}, "README.md:1: terse expand")
    # Large unranged file read in Claude Code's `N\t` line-number format.
    src = _big_python_source()
    numbered = "\n".join(f"{i}\t{ln}" for i, ln in enumerate(src.splitlines(), 1))
    t.call(450, "Read", {"file_path": "/work/proj/handlers.py"}, numbered)
    # Ranged read is never outlined.
    t.call(460, "Read", {"file_path": "/work/proj/handlers.py", "offset": 10, "limit": 400}, numbered)
    # Medium pretty-printed MCP JSON gets lossless compaction.
    rows = [{"id": i, "name": f"item-{i}", "status": "active", "owner": "team-a"} for i in range(60)]
    t.call(470, "mcp__other__list_items", {}, [{"type": "text", "text": json.dumps(rows, indent=2)}])
    t.call(480, "Glob", {"pattern": "*.py"}, "a.py\nb.py")
    return t.write(root / "projects" / "proj" / "s1.jsonl")


def _snapshot(d: Path) -> dict[str, bytes]:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name)
        self.claude = self.d / "claude"
        self.home = self.d / "terser-home"
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"outline_min_lines": 250}), encoding="utf-8")
        (self.home / "savings.jsonl").write_text('{"ts": 1, "cat": "x", "saved_tok": 5}\n', encoding="utf-8")
        self.transcript = _fixture(self.claude)
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.claude), TERSER_HOME=str(self.home))
        self.env.pop("CODEX_HOME", None)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_replay_categories(self) -> None:
        res = analytics.replay("claude", 30, env=self.env)
        cats = res["categories"]
        self.assertEqual(res["sessions"], 1)
        # npm, pytest re-run, 2x git status, terse expand, and the grep that only mentions `terse expand`.
        self.assertEqual(cats["shell"]["outputs"], 6)
        self.assertEqual(cats["terser"]["outputs"], 2)  # the marked Bash output + the terser-index call
        self.assertEqual(cats["read"]["outputs"], 2)
        self.assertEqual(cats["mcp"]["outputs"], 1)
        self.assertEqual(cats["other"]["outputs"], 1)
        self.assertGreater(cats["shell"]["avoided"], 0)
        self.assertEqual(cats["read"]["compressed"], 1)  # the unranged read only
        self.assertGreater(cats["read"]["avoided"], 0)
        self.assertGreater(cats["mcp"]["avoided"], 0)
        self.assertEqual(cats["terser"]["avoided"], 0)
        for c in cats.values():
            self.assertLessEqual(c["avoided"], c["tok_in"])

    def test_replay_is_read_only(self) -> None:
        before = _snapshot(self.home)
        prev = os.environ.get("TERSER_HOME")
        analytics.replay("claude", 30, env=self.env)
        self.assertEqual(_snapshot(self.home), before)
        self.assertEqual(os.environ.get("TERSER_HOME"), prev)

    def test_measured_outline_retry_rate_discounts_reads(self) -> None:
        gross = analytics.replay("claude", 30, env=self.env)["categories"]["read"]["avoided"]
        rows = [{"tier": "t2", "ev": "served"}] * 10 + [{"tier": "t2", "ev": "retry"}] * 5
        (self.home / "funnel.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        res = analytics.replay("claude", 30, env=self.env)
        self.assertTrue(res["outline_retry_applied"])
        self.assertAlmostEqual(res["outline_retry_rate"], 0.5)
        self.assertEqual(res["categories"]["read"]["avoided"], int(gross * 0.5))
        self.assertIn("measured outline retry rate (50% of 10)", analytics.format_replay(res))

    def test_small_retry_sample_is_flagged_not_applied(self) -> None:
        (self.home / "funnel.jsonl").write_text('{"tier":"t2","ev":"served"}\n{"tier":"t2","ev":"retry"}\n', encoding="utf-8")
        res = analytics.replay("claude", 30, env=self.env)
        self.assertFalse(res["outline_retry_applied"])
        self.assertEqual(res["categories"]["read"]["avoided"], res["categories"]["read"]["avoided_gross"])
        self.assertIn("upper bound", analytics.format_replay(res))

    def test_format_replay(self) -> None:
        text = analytics.format_replay(analytics.replay("claude", 30, env=self.env), price_in=5.0, reread_turns=2)
        for label in ("Shell output", "File reads", "MCP results", "Already via Terser", "Honest accounting"):
            self.assertIn(label, text)
        self.assertIn("input $5.00/MTok", text)
        self.assertIn("2 cache re-read(s)", text)

    def test_cli_discover_json(self) -> None:
        buf = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(buf):
            rc = cli.main(["--host", "claude", "discover", "--replay", "--days", "30", "--json"])
        self.assertEqual(rc, 0)
        res = json.loads(buf.getvalue())
        self.assertEqual(res["host"], "claude")
        self.assertGreater(res["categories"]["shell"]["avoided"], 0)
        self.assertEqual(_snapshot(self.home).keys(), {"config.json", "savings.jsonl"})


class RegretTests(unittest.TestCase):
    def test_regret_signals_from_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            st = analytics.parse_claude_transcript(_fixture(Path(td)))
        self.assertEqual(st.quick_reruns, 1)
        self.assertEqual(st.terser_reruns, 1)
        self.assertEqual(st.archive_expands, 2)
        line = analytics.regret_lines([st])[0]
        self.assertIn("1 quick identical re-run(s)", line)
        self.assertIn("1 after a Terser-compressed output", line)
        self.assertIn("2 archive expand(s)", line)

    def test_expand_detection_ignores_mentions(self) -> None:
        f = analytics._is_expand_call
        self.assertTrue(f("Bash", {"command": "cd x && terse expand ab12 1 40 | head"}))
        self.assertTrue(f("Bash", {"command": "python -X utf8 runtime/cli.py query ab12 error"}))
        self.assertTrue(f("mcp__plugin_terser_terser-index__search_sandbox", {}))
        self.assertFalse(f("Bash", {"command": 'grep -n "terse expand" README.md'}))
        self.assertFalse(f("Bash", {"command": 'echo "| terse query"'}))
        self.assertFalse(f("mcp__plugin_terser_terser-index__order", {"action": "route"}))

    def test_rerun_window(self) -> None:
        st = analytics.SessionStats(path="", host="claude")
        st.shell_runs = [[1000.0, "ls", False], [1100.0, "ls", False], [1400.0, "ls", True], [1450.0, "ls ", False]]
        self.assertEqual(st.quick_reruns, 1)  # 100 s apart counts; 300 s does not; "ls " is not byte-identical

    def test_audit_and_status_show_regret(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _fixture(d / "claude")
            (d / "home").mkdir()
            env = dict(os.environ, CLAUDE_CONFIG_DIR=str(d / "claude"), TERSER_HOME=str(d / "home"))
            with mock.patch.dict(os.environ, env, clear=True):
                text = analytics.report("claude", 30, env=env, price_in=4.0, reread_turns=5)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    cli.main(["--host", "claude", "status", "--price-in", "4", "--reread-turns", "5"])
        self.assertIn("Regret signals: 1 quick identical re-run(s)", text)
        self.assertIn("input $4.00/MTok", text)
        self.assertIn("5 cache re-read(s)", text)
        self.assertIn("Regret signals:", buf.getvalue())
        self.assertIn("5 cache re-read(s)", buf.getvalue())


class ValueModelTests(unittest.TestCase):
    def test_price_override_keeps_cached_rate_model(self) -> None:
        price = analytics.pricing({}, price_in=10.0)
        self.assertEqual(price["price_input_per_mtok"], 10.0)
        self.assertEqual(price["cache_read_mult"], analytics._PRICING_DEFAULTS["cache_read_mult"])
        # 1M tokens = 1 cache write (x1.25) + 2 re-reads (x0.1 each) at $10/MTok.
        self.assertAlmostEqual(analytics.saved_cost(1_000_000, price, 2), 10.0 * 1.45)
        self.assertEqual(analytics.pricing({"price_input_per_mtok": 7})["price_input_per_mtok"], 7.0)

    def test_cli_price_args(self) -> None:
        self.assertEqual(cli._price_args(["--price-in", "2.5", "--reread-turns=4"]), (2.5, 4.0))
        self.assertEqual(cli._price_args([]), (None, analytics.DEFAULT_REREAD_TURNS))
        self.assertEqual(cli._days_arg(["--days", "3"], 7.0), 3.0)


if __name__ == "__main__":
    unittest.main()
