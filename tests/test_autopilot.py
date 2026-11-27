"""Zero-command autopilot: MCP retrieval hints, auto-index, intent hints, context fill,
health notices, and the Stop-hook savings summary."""
from __future__ import annotations

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

from runtime import autopilot, context_sandbox, index_bridge, unified_hook  # noqa: E402

os.environ.setdefault("TERSER_PYTHON_WORKER", "0")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.env = dict(
            os.environ,
            TERSER_HOME=str(self.tmp / "home"),
            TERSER_INDEX_PATH=str(self.tmp / "idx"),
            TERSER_AUTO_INDEX="0",
        )
        self.env.pop("TERSER_HINT_HOST", None)
        self.env.pop("TERSER_CLIENT_ID", None)
        self.addCleanup(self._td.cleanup)
        self.addCleanup(context_sandbox.set_hint_host, None)


class RetrievalHintTests(_Base):
    def test_agent_hosts_get_mcp_calls_and_cli_keeps_shell_form(self) -> None:
        context_sandbox.set_hint_host(None)
        self.assertIn("terse query abc123", context_sandbox.retrieval_hint("abc123", env=self.env))
        self.assertEqual(context_sandbox.retrieval_hint("abc123", "raw", env=self.env), "`terse expand abc123 --raw`")
        for host in ("claude", "codex", "antigravity", "agent"):
            context_sandbox.set_hint_host(host)
            both = context_sandbox.retrieval_hint("abc123", env=self.env)
            self.assertIn('order(action="search_sandbox", args={"archive_id": "abc123"', both)
            self.assertIn('order(action="expand_sandbox"', both)
            self.assertNotIn("terse ", both)

    def test_mcp_server_env_selects_mcp_hints(self) -> None:
        context_sandbox.set_hint_host(None)
        hint = context_sandbox.retrieval_hint("abc123", "query", env=dict(self.env, TERSER_CLIENT_ID="claude"))
        self.assertTrue(hint.startswith('`order(action="search_sandbox"'))

    def test_dispatch_sets_hint_host(self) -> None:
        unified_hook.dispatch("Stop", {"session_id": "s"}, host_hint="codex", env=self.env)
        self.assertIn("order(", context_sandbox.retrieval_hint("x1", env=self.env))


class IntentHintTests(unittest.TestCase):
    def test_plain_language_terser_requests(self) -> None:
        self.assertIn(" status`", autopilot.intent_hint("how many tokens has terser saved?", "claude") or "")
        self.assertIn(" doctor`", autopilot.intent_hint("terser seems broken", "claude") or "")
        self.assertIn(" dashboard`", autopilot.intent_hint("open the terser dashboard", "claude") or "")
        self.assertIn("config set", autopilot.intent_hint("turn off terser compression", "claude") or "")
        self.assertIn("search_sandbox", autopilot.intent_hint("show me the full output", "claude") or "")

    def test_unrelated_and_slash_prompts_are_ignored(self) -> None:
        self.assertIsNone(autopilot.intent_hint("fix the login bug", "claude"))
        self.assertIsNone(autopilot.intent_hint("/terser:status", "claude"))
        self.assertIsNone(autopilot.intent_hint("", "claude"))

    def test_machine_turns_and_dev_requests_get_no_hint(self) -> None:
        note = (
            "<task-notification><summary>Background command \"Run terser test suite\" completed</summary>"
            "<status>completed</status></task-notification>"
        )
        self.assertIsNone(autopilot.intent_hint(note, "claude"))
        self.assertIsNone(autopilot.intent_hint("fix the terser status command output", "claude"))
        self.assertIsNone(autopilot.intent_hint("make replies terse and show status", "claude"))
        long_spec = "Improve terser. " + "Check every module for regressions in saved tokens. " * 10
        self.assertIsNone(autopilot.intent_hint(long_spec, "claude"))
        far = "terser " + "x" * 120 + " tokens"
        self.assertIsNone(autopilot.intent_hint(far, "claude"))

    def test_cli_command_is_absolute_and_quoted(self) -> None:
        cmd = autopilot.cli_command("claude")
        self.assertTrue(cmd.startswith('"') and cmd.endswith("--host claude"))
        self.assertIn("/scripts/terse", cmd)
        self.assertNotIn("\\", cmd)
        self.assertIn("via the Bash tool", autopilot.intent_hint("terser status", "claude") or "")

    def test_windows_native_shell_uses_call_operator(self) -> None:
        with mock.patch.object(autopilot.os, "name", "nt"):
            cmd = autopilot.cli_command("codex")
        self.assertTrue(cmd.startswith('& "') and cmd.endswith('terse.cmd" --host codex'))

    def test_skill_and_command_docs_have_no_stray_control_chars(self) -> None:
        for p in [*(ROOT / "skills").glob("*/SKILL.md"), *(ROOT / "commands").glob("*.md")]:
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("\t", text, p)
            self.assertRegex(text.split("---")[1], r"Use (?:when|before)", p)


class ContextFillTests(_Base):
    def _transcript(self, used: int, model: str = "claude-x") -> str:
        p = self.tmp / "t.jsonl"
        recs = [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"model": model, "usage": {"input_tokens": 5, "cache_read_input_tokens": used - 5}}},
        ]
        p.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
        return str(p)

    def test_claude_and_codex_fill(self) -> None:
        self.assertAlmostEqual(autopilot.context_fill(self._transcript(150_000), "claude") or 0, 0.75)
        self.assertAlmostEqual(autopilot.context_fill(self._transcript(500_000, "claude-x[1m]"), "claude") or 0, 0.5)
        codex = self.tmp / "c.jsonl"
        codex.write_text(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 900}, "model_context_window": 1000}}}) + "\n", encoding="utf-8")
        self.assertAlmostEqual(autopilot.context_fill(str(codex), "codex") or 0, 0.9)
        self.assertIsNone(autopilot.context_fill(str(self.tmp / "missing"), "claude"))

    def test_prompt_submit_emits_fill_nudge_once(self) -> None:
        payload = {"session_id": "fill-s", "prompt": "continue", "transcript_path": self._transcript(150_000)}
        out = unified_hook.dispatch("UserPromptSubmit", payload, host_hint="claude", env=self.env)
        ctx = ((out or {}).get("hookSpecificOutput") or {}).get("additionalContext") or ""
        self.assertIn(">=70% full", ctx)
        again = unified_hook.dispatch("UserPromptSubmit", payload, host_hint="claude", env=self.env)
        self.assertEqual(again, {})
        payload["transcript_path"] = self._transcript(190_000)
        out = unified_hook.dispatch("UserPromptSubmit", payload, host_hint="claude", env=self.env)
        self.assertIn(">=85% full", out["hookSpecificOutput"]["additionalContext"])

    def test_prompt_submit_injects_intent_hint(self) -> None:
        out = unified_hook.dispatch(
            "UserPromptSubmit", {"session_id": "int-s", "prompt": "is terser working?"}, host_hint="claude", env=self.env
        )
        self.assertIn("doctor", out["hookSpecificOutput"]["additionalContext"])


class AutoIndexTests(_Base):
    def test_spawn_auto_index_guards_and_cooldown(self) -> None:
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        plain = self.tmp / "plain"
        plain.mkdir()
        with mock.patch.object(index_bridge, "_spawn_index_cmd", return_value=True) as spawn:
            self.assertFalse(index_bridge.spawn_auto_index(str(plain), env=self.env))
            self.assertTrue(index_bridge.spawn_auto_index(str(repo), max_files=42, env=self.env))
            args = spawn.call_args[0][0]
            self.assertEqual(args[0], "auto-index")
            self.assertEqual(args[2], "42")
            # Cooldown: no respawn within the window
            self.assertFalse(index_bridge.spawn_auto_index(str(repo), env=self.env))
            self.assertEqual(spawn.call_count, 1)

    def test_subfolder_session_indexes_enclosing_git_root(self) -> None:
        repo = self.tmp / "mono"
        (repo / ".git").mkdir(parents=True)
        sub = repo / "pkg" / "app"
        sub.mkdir(parents=True)
        with mock.patch.object(index_bridge, "_spawn_index_cmd", return_value=True) as spawn:
            self.assertTrue(index_bridge.spawn_auto_index(str(sub), env=self.env))
        self.assertEqual(Path(spawn.call_args[0][0][1]), repo.resolve())

    def test_session_start_spawns_and_prompt_announces_ready(self) -> None:
        repo = self.tmp / "repo2"
        (repo / ".git").mkdir(parents=True)
        env = dict(self.env)
        env.pop("TERSER_AUTO_INDEX")
        with mock.patch.object(index_bridge, "spawn_auto_index", return_value=True):
            out = unified_hook.dispatch(
                "SessionStart", {"session_id": "ai-s", "cwd": str(repo), "source": "startup"}, host_hint="claude", env=env
            )
        self.assertIn("indexing repo in background", out.get("systemMessage") or "")
        fake = {"repo": "repo2", "files": 1, "symbols": 3}
        with mock.patch.object(index_bridge, "workspace_index_summary", return_value=fake):
            out = unified_hook.dispatch("UserPromptSubmit", {"session_id": "ai-s", "prompt": "go"}, host_hint="claude", env=env)
            self.assertIn("is now ready", out["hookSpecificOutput"]["additionalContext"])
            again = unified_hook.dispatch("UserPromptSubmit", {"session_id": "ai-s", "prompt": "go"}, host_hint="claude", env=env)
        self.assertEqual(again, {})


class HealthTests(_Base):
    def test_health_notice_only_on_failure(self) -> None:
        self.assertIsNone(autopilot.health_notice("claude", self.env))
        with mock.patch.object(autopilot, "quick_health", return_value=["analytics engine is missing"]):
            msg = autopilot.health_notice("claude", self.env)
        self.assertIn("Terser degraded: analytics engine is missing", msg or "")
        self.assertIn("doctor", msg or "")

    def test_terser_mcp_failure_hint(self) -> None:
        self.assertIn("doctor", autopilot.mcp_failure_hint("mcp__plugin_terser_terser-index__order", "claude") or "")
        self.assertIsNone(autopilot.mcp_failure_hint("mcp__github__search", "claude"))
        out = unified_hook.dispatch(
            "PostToolUseFailure",
            {"session_id": "mf", "tool_name": "mcp__plugin_terser_terser-index__order", "tool_input": {}, "error": "boom"},
            host_hint="claude",
            env=self.env,
        )
        self.assertIn("Terser index MCP call failed", out["hookSpecificOutput"]["additionalContext"])


class SessionSummaryTests(_Base):
    def _savings(self, *pairs: tuple[float, int]) -> None:
        log = Path(self.env["TERSER_HOME"]) / "savings.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            for ts, saved in pairs:
                fh.write(json.dumps({"ts": ts, "cat": "x", "saved_tok": saved}) + "\n")

    def test_milestones(self) -> None:
        self.assertFalse(autopilot.next_milestone(4999, 0))
        self.assertTrue(autopilot.next_milestone(5000, 0))
        self.assertFalse(autopilot.next_milestone(9000, 5000))
        self.assertTrue(autopilot.next_milestone(10000, 5000))

    def test_stop_shows_session_savings_at_milestones_only(self) -> None:
        unified_hook.dispatch("SessionStart", {"session_id": "sum-s", "cwd": str(self.tmp)}, host_hint="claude", env=self.env)
        start = time.time()
        self._savings((start - 1000, 999_999))  # earlier session: excluded
        stop = {"session_id": "sum-s"}
        self.assertEqual(unified_hook.dispatch("Stop", stop, host_hint="claude", env=self.env), {})
        self._savings((start + 1, 6000))
        out = unified_hook.dispatch("Stop", stop, host_hint="claude", env=self.env)
        self.assertEqual(out, {"systemMessage": "Terser saved ~6,000 tokens this session."})
        self.assertEqual(unified_hook.dispatch("Stop", stop, host_hint="claude", env=self.env), {})
        self.assertEqual(unified_hook.dispatch("SessionEnd", stop, host_hint="claude", env=self.env), {})


if __name__ == "__main__":
    unittest.main()
