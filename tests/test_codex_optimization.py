import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime import cmd_delta, config, context_sandbox, index_mcp, session_brief, unified_hook

# Hook subprocesses in tests must not leave warm workers behind (each test has its own TERSER_HOME).
os.environ.setdefault("TERSER_PYTHON_WORKER", "0")


ROOT = Path(__file__).resolve().parents[1]


class CodexOptimizationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.env = dict(os.environ, TERSER_HOME=str(self.root / "state"), TERSER_INDEX_PATH=str(self.root / "index"))
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.source = self.repo / "example.py"
        self.source.write_text(
            "class Example:\n    def first(self):\n"
            + "".join(f"        value_{i} = {i}\n" for i in range(25))
            + "        return value_24\n\n    def second(self):\n        return 2\n",
            encoding="utf-8",
        )
        self.slug = index_mcp.index_folder(str(self.repo), env=self.env)["repo"]
        self.symbols = index_mcp.dispatch_action(
            "get_file_outline", {"repo": self.slug, "file_path": self.source.name}, env=self.env,
        )["symbols"]

    def _call(self, *args):
        return subprocess.run([sys.executable, "-B", *map(str, args)], cwd=ROOT, env=self.env,
                              capture_output=True, timeout=15)

    def test_source_batches_preserve_shared_lines_and_coordinates(self):
        with patch.dict(os.environ, self.env, clear=True):
            for action, extra in (
                ("get_symbols", {"symbol_ids": [s["id"] for s in self.symbols]}),
                ("assemble_task_context", {"task": "example first second"}),
            ):
                reply = index_mcp._handle_request({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                        "name": "order", "arguments": {"action": action, "args": {"repo": self.slug, **extra}},
                    },
                })
                text = reply["result"]["content"][0]["text"]
                lines = self.source.read_text(encoding="utf-8").splitlines()
                for symbol in self.symbols:
                    start, end = symbol["start_line"], symbol["end_line"]
                    with self.subTest(action=action, symbol=symbol["id"]):
                        self.assertIn(f"{symbol['id']} ({self.source.name} L{start}-{end})", text)
                        self.assertIn("\n".join(lines[start - 1:end]), text)
                self.assertEqual(text.count("value_24 = 24"), 1)

            batch = index_mcp.dispatch_action("get_symbols", {
                "repo": self.slug, "symbol_ids": [s["id"] for s in self.symbols],
            }, env=self.env)
            batch["symbols"].append({"error": "Symbol changed in another file."})
            text = index_mcp.format_mcp_response("get_symbols", batch)
            self.assertIn("Symbol changed in another file.", text)
            self.assertEqual(text.count("value_24 = 24"), 1)

    def test_mcp_reuse_requires_visible_success_and_current_revision(self):
        transcript = self.root / "rollout.jsonl"
        tool_input = {"action": "get_symbol_source", "args": {
            "repo": self.slug, "symbol_id": self.symbols[0]["id"], "reuse_visible_result": True,
        }}
        result = index_mcp.dispatch_action("get_symbol_source", tool_input["args"], env=self.env)
        text = index_mcp.format_mcp_response("get_symbol_source", result, revision=True)
        base = {"session_id": "main", "turn_id": "turn", "cwd": str(self.repo),
                "transcript_path": str(transcript), "tool_name": "mcp__terser_index__order", "tool_input": tool_input}
        post = dict(base, tool_use_id="first", tool_response={"content": [{"type": "text", "text": text}]})

        def hook(event, data):
            return unified_hook.dispatch(event, data, host_hint="codex", env=self.env)

        pre = dict(base, tool_use_id="second")
        self.assertIsNone(hook("PreToolUse", dict(base, tool_use_id="first")))
        self.assertIsNone(hook("PreToolUse", pre))
        self.assertEqual(hook("PostToolUse", post), {})
        self.assertIsNone(hook("PreToolUse", pre))
        delivered = {"type": "response_item", "payload": {
            "type": "mcp_tool_call_output", "call_id": "first", "output": post["tool_response"],
        }}
        transcript.write_text(json.dumps(delivered) + "\n", encoding="utf-8")
        self.assertIsNone(hook("PreToolUse", dict(pre, agent_id="child")))
        self.assertIsNone(hook("PreToolUse", dict(pre, turn_id="other")))
        original = self.source.read_bytes()
        with self.source.open("a", encoding="utf-8") as handle:
            handle.write("\n# A change outside the selected symbol\n")
        self.assertIsNone(hook("PreToolUse", pre))
        self.source.write_bytes(original)
        self.assertEqual(hook("PreToolUse", pre)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(hook("PreToolUse", dict(pre, tool_use_id="retry")))
        hook("PostToolUse", post)
        hook("PostCompact", base)
        self.assertIsNone(hook("PreToolUse", dict(pre, tool_use_id="after-compact")))

    def test_exact_delta_keys_and_raw_archive_recovery(self):
        log = "\n".join(f"test_{i}: consistent output" for i in range(40))
        command = 'pytest -k "foo  bar"'

        def delta(name=command, cwd=self.repo, env=self.env):
            return cmd_delta.apply_command_delta("session", name, log, host="codex", cwd=str(cwd), env=env)

        self.assertEqual(delta(), (log, False))
        self.assertEqual(delta('pytest -k "foo bar"'), (log, False))
        self.assertEqual(delta(cwd=self.root), (log, False))
        self.assertEqual(delta(env=dict(self.env, PYTEST_ADDOPTS="--quiet")), (log, False))
        short, used = delta()
        self.assertTrue(used)
        archive = re.search(r"terse expand ([0-9a-f]+) --raw", short)[1]
        self.assertEqual(context_sandbox.read_stored_output(archive, env=self.env), log)
        self.assertEqual(self._call(ROOT / "runtime/cli.py", "expand", archive, "--raw").stdout, log.encode())

    def test_command_entry_points_keep_failure_diagnostics(self):
        program = "import sys; print('src/worker.py:47: detail' * 800); print('original warning', file=sys.stderr); sys.exit(23)"
        proc = self._call(ROOT / "runtime/cli.py", "exec", "--sid", "scope", "--", sys.executable, "-c", program)
        self.assertEqual(proc.returncode, 23)
        self.assertEqual(proc.stdout.splitlines(), [b"src/worker.py:47: detail" * 800])
        self.assertEqual(proc.stderr.splitlines(), [b"original warning"])
        result = index_mcp.dispatch_action("execute_filtered", {"argv": [sys.executable, "-c", program]}, env=self.env)
        response = index_mcp.format_mcp_response("execute_filtered", result)
        self.assertIn("exit=23", response)
        self.assertIn("src/worker.py:47: detail" * 800, response)
        self.assertIn("original warning", response)

    def test_filtered_exec_keeps_large_output_when_intent_has_no_matches(self):
        program = "import sys; assert not sys.stdin.read(); print('original stdout evidence' * 300)"
        result = index_mcp.dispatch_action("execute_filtered", {
            "argv": [sys.executable, "-c", program], "intent": "absent_search_term"
        }, env=self.env)
        self.assertEqual(result["returncode"], 0)
        self.assertFalse(result["filtered"])
        self.assertEqual(result["output"], "original stdout evidence" * 300 + "\n")

    def test_startup_keeps_only_new_facts_and_continuity(self):
        transcript = self.root / "rollout.jsonl"
        messages = {
            "user": "# AGENTS.md instructions for workspace\n<INSTRUCTIONS>\n- Use the checked-in fixtures.\n</INSTRUCTIONS>",
            "developer": "<skills_instructions>\n- Read only the needed sections.\n</skills_instructions>",
        }
        transcript.write_text("\n".join(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": role, "content": [{"text": text}],
        }}) for role, text in messages.items()), encoding="utf-8")
        with (
            patch("runtime.session_brief._load_microskills", return_value=[
                "- Use the checked-in fixtures.", "- Read only the needed sections.", "- Local fact.",
            ]),
            patch("runtime.session_brief._load_continuity", return_value=["- Local fact.", "- Resume the renderer task."]),
        ):
            reply = unified_hook.handle_session_start("codex", {"session_id": "startup", "cwd": str(self.repo),
                "transcript_path": str(transcript)}, config.load_config(self.env), self.env)
        text = reply["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("checked-in fixtures", text)
        self.assertNotIn("needed sections", text)
        self.assertEqual(text.count("Local fact."), 1)
        self.assertIn("Resume the renderer task.", text)

    def test_startup_reserves_room_for_complete_continuity(self):
        limit = 300
        with (
            patch("runtime.session_brief._load_microskills", return_value=["- " + "x" * 180, "- Use local fixtures."]),
            patch("runtime.session_brief.workspace_index_summary", return_value=None),
            patch("runtime.repo_map.build_repo_map", return_value="[repo map]\n" + "a.py: func\n" * 40),
            patch("runtime.session_brief._load_continuity", return_value=["- Resume: " + "y" * 160, "- Rerun smoke tests."]),
        ):
            brief, notice = session_brief.build_session_brief(
                str(self.repo), host="codex", cfg=dict(config.DEFAULT_CONFIG, session_brief_max_chars=limit), env=self.env,
            )
        self.assertLessEqual(len(brief), limit)
        self.assertIn("- Use local fixtures.", brief)
        self.assertIn("- Resume: " + "y" * 160, brief)
        self.assertIn("- Rerun smoke tests.", brief)
        self.assertNotIn("[repo map]", brief)
        self.assertIn("1 repo fact(s)", notice)

    def test_rollout_comparison_uses_cumulative_usage_and_splits_cache(self):
        before = self.root / "before.jsonl"
        after = self.root / "after.jsonl"
        for path, input_tokens in ((before, 1000), (after, 600)):
            events = [
                {"type": "turn_context", "payload": {"model": "fixture"}},
                {"type": "event_msg", "payload": {"type": "user_message", "message": "Read the fixture."}},
                {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": input_tokens, "cached_input_tokens": 100,
                    "output_tokens": 200, "reasoning_output_tokens": 120,
                }}}},
            ]
            path.write_text("\n".join(map(json.dumps, events)), encoding="utf-8")
        run = self._call(ROOT / "runtime/cli.py", "--host", "codex", "benchmark", "--runs", "1",
                         "--codex-rollouts", before, after)
        self.assertEqual(run.returncode, 0, run.stderr)
        rows = run.stdout.decode().splitlines()[-4:]
        self.assertEqual(rows[0].split()[-3:], ["900", "500", "-400"])
        self.assertEqual(rows[1].split()[-3:], ["100", "100", "+0"])
        self.assertEqual(rows[3].split()[-3:], ["200", "200", "+0"])
