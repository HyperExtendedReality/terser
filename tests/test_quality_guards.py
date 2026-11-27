"""Output-quality guards: MCP refetch scope, Grep answers, routed-subagent double check, MCP arg coercion."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from runtime import index_bridge, index_mcp, read_funnel, subagent_verify, unified_hook  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.env = {"TERSER_HOME": str(self.tmp / "home")}
        self._patch = mock.patch.dict(os.environ, self.env)
        self._patch.start()
        self.cfg = {"mcp_refetch_guard": True, "mcp_dedup_window_s": 120}

    def tearDown(self) -> None:
        self._patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class McpRefetchTests(_Base):
    def test_live_servers_and_ui_tools_are_never_deduped(self) -> None:
        for name in (
            "mcp__Claude_Browser__find",
            "mcp__claude-in-chrome__get_page_text",
            "mcp__computer-use__zoom",
            "mcp__db__run_query",
        ):
            args = {"q": 1}
            self.assertIsNone(read_funnel.check_mcp_refetch("s-live", name, args, cfg=self.cfg, env=self.env))
            self.assertIsNone(read_funnel.check_mcp_refetch("s-live", name, args, cfg=self.cfg, env=self.env), name)

    def test_mutating_call_and_edit_clear_earlier_reads(self) -> None:
        name, args = "mcp__docs__get_docs", {"lib": "x"}
        self.assertIsNone(read_funnel.check_mcp_refetch("s-mut", name, args, cfg=self.cfg, env=self.env))
        read_funnel.check_mcp_refetch("s-mut", "mcp__docs__update_docs", {}, cfg=self.cfg, env=self.env)
        self.assertIsNone(read_funnel.check_mcp_refetch("s-mut", name, args, cfg=self.cfg, env=self.env))
        f = self.tmp / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        read_funnel.invalidate_on_edit("s-mut", str(f), env=self.env)
        self.assertIsNone(read_funnel.check_mcp_refetch("s-mut", name, args, cfg=self.cfg, env=self.env))
        # Still dedups an identical static read with nothing in between.
        res = read_funnel.check_mcp_refetch("s-mut", name, args, cfg=self.cfg, env=self.env)
        self.assertEqual((res or {}).get("action"), "deny")


class GrepAnswerTests(_Base):
    def _fake(self, root: str, hits: int, text: str = "needle here"):
        idx = {"repo": "r", "source_root": root}
        res = {"matches": [{"file": f"f{i}.py", "line": i, "text": text} for i in range(hits)]}
        return (
            mock.patch.object(index_bridge, "workspace_index_summary", return_value=idx),
            mock.patch.object(index_mcp, "dispatch_action", return_value=res),
        )

    def test_answers_once_then_real_grep_runs(self) -> None:
        root = index_bridge.normalize_workspace(str(self.tmp))
        a, b = self._fake(root, 30)
        with a, b:
            first = index_bridge.try_answer_large_grep({"pattern": "needle"}, str(self.tmp), "sid-g")
            second = index_bridge.try_answer_large_grep({"pattern": "needle"}, str(self.tmp), "sid-g")
        self.assertIn("repeat this exact Grep", first or "")
        self.assertIsNone(second)

    def test_scoped_regex_case_and_foreign_root_pass_through(self) -> None:
        root = index_bridge.normalize_workspace(str(self.tmp))
        a, b = self._fake(root, 30)
        with a, b:
            for ti in ({"pattern": "needle", "glob": "*.ts"}, {"pattern": "needle", "-i": True},
                       {"pattern": "need.e"}, {"pattern": "needle", "path": str(self.tmp / "sub")},
                       {"pattern": "needle", "output_mode": "count"}):
                self.assertIsNone(index_bridge.try_answer_large_grep(ti, str(self.tmp), "sid-s"), ti)
            self.assertIsNone(index_bridge.try_answer_large_grep({"pattern": "needle"}, str(self.tmp.parent), "sid-p"))
        a, b = self._fake(root, 30, text="NEEDLE upper")
        with a, b:
            self.assertIsNone(index_bridge.try_answer_large_grep({"pattern": "needle"}, str(self.tmp), "sid-c"))


class RoutingTests(_Base):
    def test_should_route_vetoes(self) -> None:
        self.assertTrue(subagent_verify.should_route("find where parse_args is defined", "Explore", "")[0])
        self.assertFalse(subagent_verify.should_route("find where parse_args is defined", "Explore", "opus")[0])
        self.assertFalse(subagent_verify.should_route("fix the typo in readme", "general-purpose", "")[0])
        self.assertFalse(subagent_verify.should_route("find where auth tokens are checked", "Explore", "")[0])
        self.assertFalse(subagent_verify.should_route("find where x is", "Plan", "")[0])

    def test_pretool_routes_with_contract_and_post_flags_bad_citation(self) -> None:
        cfg = {"subagent_model_routing": True, "fanout_checkpoint": False}
        payload = {"session_id": "rt", "tool_name": "Agent", "cwd": str(self.tmp),
                   "tool_input": {"subagent_type": "Explore", "prompt": "find where parse_args is defined"}}
        with mock.patch.object(unified_hook, "load_config", return_value=cfg, create=True):
            out = unified_hook.handle_pre_tool_use("claude", payload, cfg, self.env)
        upd = out["hookSpecificOutput"]["updatedInput"]
        self.assertEqual(upd["model"], "haiku")
        self.assertIn(subagent_verify.MARKER, upd["prompt"])

        (self.tmp / "cli.py").write_text("import sys\n\ndef parse_args():\n    pass\n", encoding="utf-8")
        good = "`parse_args` is defined at cli.py:3."
        self.assertIsNone(subagent_verify.check_routed_result(upd, {"content": [{"type": "text", "text": good}]}, str(self.tmp), self.env))
        bad = "`parse_args` is defined at cli.py:300 and `load_all` at missing.py:2."
        note = subagent_verify.check_routed_result(upd, bad, str(self.tmp), self.env)
        self.assertIn("failed the double check", note or "")
        esc = subagent_verify.check_routed_result(upd, "ESCALATE: needs design judgment", str(self.tmp), self.env)
        self.assertIn("escalated", esc or "")
        self.assertEqual(subagent_verify.stats(self.env)["checked"], 3)

    def test_unrouted_agents_are_not_checked(self) -> None:
        self.assertIsNone(subagent_verify.check_routed_result({"prompt": "x"}, "anything", str(self.tmp), self.env))

    def test_backoff_after_repeated_failures(self) -> None:
        log = self.tmp / "home" / "routing.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        import time
        log.write_text("".join(json.dumps({"ts": time.time(), "event": "verified", "ok": i < 4}) + "\n" for i in range(10)), encoding="utf-8")
        self.assertTrue(subagent_verify.stats(self.env)["backed_off"])
        ok, why = subagent_verify.should_route("find where parse_args is defined", "Explore", "", self.env)
        self.assertFalse(ok)
        self.assertIn("backoff", why)


class McpArgCoercionTests(unittest.TestCase):
    def test_stringified_args_and_numbers_are_accepted(self) -> None:
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "order", "arguments": {"action": "menu", "args": "{}"}}}
        out = index_mcp._handle_request(req)
        self.assertNotIn("error", out)
        self.assertEqual(index_mcp._coerce_order_args({"limit": "5", "symbol_ids": "a,b"}),
                         {"limit": 5, "symbol_ids": ["a", "b"]})

    def test_unknown_action_suggests_close_match(self) -> None:
        _act, payload = index_mcp._handle_tool_call("order", {"action": "serch_symbols", "args": {}})
        self.assertIn("search_symbols", payload["error"])


if __name__ == "__main__":
    unittest.main()
