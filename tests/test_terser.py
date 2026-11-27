"""Comprehensive unit & integration tests for Terser."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime import (
    cmd_delta,
    config,
    config_auditor,
    context_sandbox,
    loop_guard,
    output_compressor,
    read_funnel,
    repo_map,
    session_brief,
    smart_crusher,
    task_router,
    unified_hook,
)

# Hook subprocesses in tests must not leave warm workers behind (each test has its own TERSER_HOME).
os.environ.setdefault("TERSER_PYTHON_WORKER", "0")


class OutputCompressorTests(unittest.TestCase):
    def test_strip_ansi_and_progress_spinners(self) -> None:
        raw = "\x1b[32mBuilding\x1b[0m \x1b]8;https://example.com\x07DocsLink\x1b]8;;\x07\nDownloading 10%\rDownloading 50%\rDownloading 100%\nDone!\n"
        cleaned = output_compressor.strip_ansi_and_progress(raw)
        self.assertEqual(cleaned, "Building DocsLink\nDownloading 100%\nDone!\n")

    def test_vendor_bash_compress_git_status_and_tee_on_failure(self) -> None:
        raw = (
            "On branch main\n"
            "Your branch is up to date with 'origin/main'.\n\n"
            "Changes not staged for commit:\n"
            "  (use \"git add <file>...\" to update what will be committed)\n"
            "  (use \"git restore <file>...\" to discard changes in working directory)\n"
            + "".join(f"\tmodified:   src/module_{i}.py\n" for i in range(25))
        )
        comp, stats = output_compressor.compress_output(raw, command="git status", returncode=0)
        self.assertLess(stats["served_chars"], stats["raw_chars"])
        # Tee-on-failure: non-zero exit must preserve raw output via vendor handler
        fail_raw = "fatal: not a git repository (or any of the parent directories): .git\n" + ("detail line\n" * 10)
        comp_fail, _ = output_compressor.compress_output(fail_raw, command="git status", returncode=128)
        self.assertIn("fatal: not a git repository", comp_fail)

    def test_collapse_duplicate_lines_preserves_errors(self) -> None:
        lines = [
            "2026-11-24T10:00:01 INFO polling worker ok",
            "2026-11-24T10:00:02 INFO polling worker ok",
            "2026-11-24T10:00:03 INFO polling worker ok",
            "2026-11-24T10:00:04 INFO polling worker ok",
            "ERROR: connection refused on port 5432",
            "ERROR: connection refused on port 5432",
            "ERROR: connection refused on port 5432",
        ]
        out = output_compressor.collapse_duplicate_lines("\n".join(lines))
        self.assertIn("[... repeated 3x more ...]", out)
        # All 3 ERROR lines must be preserved verbatim
        self.assertEqual(out.count("ERROR: connection refused on port 5432"), 3)

    def test_lossless_columnar_json_folding(self) -> None:
        items = [
            {"id": i, "name": f"module_{i}", "status": "passed", "duration_ms": i * 3}
            for i in range(40)
        ]
        raw = json.dumps(items, indent=2)
        folded = output_compressor.fold_uniform_json(raw)
        self.assertIsNotNone(folded)
        assert folded is not None
        self.assertLess(len(folded), len(raw) * 0.6)
        parsed = json.loads(folded)
        self.assertEqual(parsed["_format"], "columnar")
        self.assertEqual(parsed["cols"], ["id", "name", "status", "duration_ms"])
        self.assertEqual(len(parsed["rows"]), 40)
        # Verify 100% lossless round-trip
        reconstructed = [dict(zip(parsed["cols"], r)) for r in parsed["rows"]]
        self.assertEqual(reconstructed, items)

    def test_error_pinned_window_and_archive_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = {"TERSER_HOME": td}
            body = ["routine build output line " + str(i) for i in range(600)]
            body[300] = "FAILED tests/test_auth.py::test_login - AssertionError: 401 != 200"
            raw = "\n".join(body)
            comp, stats = output_compressor.compress_output(raw, command="pytest", max_chars=2500, env=env)
            self.assertLessEqual(len(comp), 2600)
            self.assertIn("FAILED tests/test_auth.py::test_login", comp)
            self.assertIsNotNone(stats["archive_id"])
            archived = output_compressor.read_archived_output(stats["archive_id"], env=env)
            self.assertIsNotNone(archived)
            assert archived is not None
            self.assertIn("FAILED tests/test_auth.py::test_login", archived)

    def test_stream_filters_cargo_and_go_preserve_errors(self) -> None:
        cargo_raw = (
            "".join(f"   Compiling crate_{i} v0.1.0\n" for i in range(15))
            + "error[E0308]: mismatched types\n  --> src/main.rs:42:5\n"
        )
        out = output_compressor.apply_stream_filters(cargo_raw, "cargo build")
        self.assertIn("compile/download status lines omitted", out)
        self.assertIn("error[E0308]: mismatched types", out)

        go_raw = (
            "".join(f"=== RUN   TestCase{i}\n--- PASS: TestCase{i} (0.00s)\n" for i in range(12))
            + "--- FAIL: TestAuth (0.02s)\nFAIL\tpkg/auth\t0.15s\n"
        )
        go_out = output_compressor.apply_stream_filters(go_raw, "go test ./...")
        self.assertIn("passing RUN/PASS lines omitted", go_out)
        self.assertIn("--- FAIL: TestAuth", go_out)

    def test_wrapper_rejects_non_whitelisted_command(self) -> None:
        self.assertFalse(output_compressor._recheck_safe_argv(["rm", "-rf", "/"]))
        self.assertFalse(output_compressor._recheck_safe_argv(["python", "-c", "import os"]))


class ReadFunnelTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.env = {"TERSER_HOME": str(self.tmp / "state")}
        self.cfg = config.load_config(self.env)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_tier0_unchanged_reread_and_retry_escape(self) -> None:
        f = self.tmp / "small.py"
        f.write_text("def hello():\n    return 42\n", encoding="utf-8")
        # First read passes
        self.assertIsNone(read_funnel.check_read("sess-1", str(f), cfg=self.cfg, env=self.env))
        # Second unchanged read soft-denies ONCE
        res = read_funnel.check_read("sess-1", str(f), cfg=self.cfg, env=self.env)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res["action"], "deny")
        self.assertIn("unchanged", res["reason"])
        # Immediate retry passes through!
        self.assertIsNone(read_funnel.check_read("sess-1", str(f), cfg=self.cfg, env=self.env))

    def test_tier1_delta_diff_on_small_edit(self) -> None:
        f = self.tmp / "mod.py"
        lines = [f"x_{i} = {i}\n" for i in range(60)]
        f.write_text("".join(lines), encoding="utf-8")
        self.assertIsNone(read_funnel.check_read("sess-2", str(f), cfg=self.cfg, env=self.env))
        lines[30] = "x_30 = 999\n"
        f.write_text("".join(lines), encoding="utf-8")
        res = read_funnel.check_read("sess-2", str(f), cfg=self.cfg, env=self.env)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res["action"], "deny")
        self.assertIn("-x_30 = 30", res["reason"])
        self.assertIn("+x_30 = 999", res["reason"])

    def test_tier2_outline_first_large_file_and_retry_full(self) -> None:
        f = self.tmp / "large_service.py"
        blocks = []
        for i in range(45):
            blocks.append(
                f"def handler_{i}(req, ctx):\n"
                f"    '''Handle request number {i} with detailed validation logic.'''\n"
                f"    payload = req.get('data_{i}', '')\n"
                f"    result = f'processed-{{payload}}-{i}-padding-for-realistic-file-size'\n"
                f"    if not result:\n"
                f"        raise ValueError('empty')\n"
                f"    return {{'id': {i}, 'value': result}}\n\n"
            )
        f.write_text("".join(blocks), encoding="utf-8")
        self.assertGreater(f.stat().st_size, 10240)
        # First unranged read returns line-numbered outline ONCE
        res = read_funnel.check_read("sess-3", str(f), cfg=self.cfg, host="antigravity", env=self.env)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertEqual(res["action"], "deny")
        self.assertIn("Outline", res["reason"])
        self.assertIn("handler_0", res["reason"])
        self.assertIn("StartLine/EndLine", res["reason"])
        # Immediate retry loads the full file!
        self.assertIsNone(read_funnel.check_read("sess-3", str(f), cfg=self.cfg, host="antigravity", env=self.env))

    def test_tier2b_markdown_heading_outline_and_retry_full(self) -> None:
        f = self.tmp / "HUGE_GUIDE.md"
        sections = []
        for i in range(30):
            sections.append(
                f"## Section {i}: Architecture Details\n\n"
                + ("Detailed documentation paragraph explaining token savings and design.\n" * 8)
                + "\n"
            )
        f.write_text("# Main Guide\n\n" + "".join(sections), encoding="utf-8")
        self.assertGreater(f.stat().st_size, 10240)
        res = read_funnel.check_read("sess-md", str(f), cfg=self.cfg, host="antigravity", env=self.env)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("## Section 0: Architecture Details", res["reason"])
        # Immediate retry loads the full doc
        self.assertIsNone(read_funnel.check_read("sess-md", str(f), cfg=self.cfg, host="antigravity", env=self.env))

    def test_outline_backoff_guardrail_prevents_extra_turns(self) -> None:
        # After 2 full-file outline overrides in one session, the 3rd large file passes straight through
        files = []
        for idx in range(3):
            f = self.tmp / f"big_{idx}.py"
            f.write_text(
                "".join(
                    f"def func_{idx}_{j}(x):\n    '''Docstring padding for file {idx} fn {j} realistic size.'''\n    return x + {j}\n\n"
                    for j in range(130)
                ),
                encoding="utf-8",
            )
            self.assertGreater(f.stat().st_size, 10240)
            files.append(f)
        sid = "sess-backoff"
        # File 0: outline -> retry full (override #1)
        self.assertIsNotNone(read_funnel.check_read(sid, str(files[0]), cfg=self.cfg, env=self.env))
        self.assertIsNone(read_funnel.check_read(sid, str(files[0]), cfg=self.cfg, env=self.env))
        # File 1: outline -> retry full (override #2 -> reaches outline_backoff_limit=2)
        self.assertIsNotNone(read_funnel.check_read(sid, str(files[1]), cfg=self.cfg, env=self.env))
        self.assertIsNone(read_funnel.check_read(sid, str(files[1]), cfg=self.cfg, env=self.env))
        # File 2: backoff active -> passes straight through on FIRST read!
        self.assertIsNone(read_funnel.check_read(sid, str(files[2]), cfg=self.cfg, env=self.env))

    def test_ranged_read_bypasses_outline_first(self) -> None:
        f = self.tmp / "large_ranged.py"
        f.write_text("\n".join(f"def fn_{i}():\n    return {i} * 1234567890" for i in range(200)), encoding="utf-8")
        self.assertIsNone(
            read_funnel.check_read("sess-4", str(f), start_line=1, end_line=40, cfg=self.cfg, env=self.env)
        )

    def test_mcp_refetch_guard(self) -> None:
        args = {"repo": "app", "query": "auth"}
        self.assertIsNone(read_funnel.check_mcp_refetch("sess-5", "terser-index:search_symbols", args, cfg=self.cfg, env=self.env))
        # Duplicate within window soft-denies once
        res = read_funnel.check_mcp_refetch("sess-5", "terser-index:search_symbols", args, cfg=self.cfg, env=self.env)
        self.assertIsNotNone(res)
        # Mutating tools are never blocked
        self.assertIsNone(read_funnel.check_mcp_refetch("sess-5", "terser-index:index_folder", args, cfg=self.cfg, env=self.env))
        self.assertIsNone(read_funnel.check_mcp_refetch("sess-5", "terser-index:index_folder", args, cfg=self.cfg, env=self.env))


class LoopGuardAndHostDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.env = {"TERSER_HOME": str(self.tmp / "state")}
        self.cfg = config.load_config(self.env)

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_loop_guard_distinct_edits_not_flagged_as_thrash(self) -> None:
        sid = "sess-edits"
        for i in range(4):
            args = {"Path": "foo.py", "Target": f"def fn_{i}(): pass", "Replacement": f"def fn_{i}(): return {i}"}
            nudge = loop_guard.record_tool_event(sid, "edit_file", args, is_error=False, cfg=self.cfg, now=200 + i, env=self.env)
            self.assertIsNone(nudge, "Distinct edits to different regions of the same file must not trigger thrash")

    def test_codex_apply_patch_invalidates_multiple_files(self) -> None:
        f1 = self.tmp / "a.py"
        f2 = self.tmp / "b.py"
        f1.write_text("x = 1\n", encoding="utf-8")
        f2.write_text("y = 2\n", encoding="utf-8")
        sid = "codex-patch"
        # Prime read cache + trigger 1 soft-deny on both files
        self.assertIsNone(read_funnel.check_read(sid, str(f1), cfg=self.cfg, host="codex", env=self.env))
        self.assertIsNotNone(read_funnel.check_read(sid, str(f1), cfg=self.cfg, host="codex", env=self.env))
        self.assertIsNone(read_funnel.check_read(sid, str(f2), cfg=self.cfg, host="codex", env=self.env))
        self.assertIsNotNone(read_funnel.check_read(sid, str(f2), cfg=self.cfg, host="codex", env=self.env))
        # Codex apply_patch edits both files
        patch = f"*** Begin Patch\n*** Update File: {f1}\n@@\n-x = 1\n+x = 10\n*** Update File: {f2}\n@@\n-y = 2\n+y = 20\n*** End Patch\n"
        unified_hook.dispatch(
            "PostToolUse",
            {"session_id": sid, "tool_name": "apply_patch", "tool_input": {"input": patch}},
            host_hint="codex", env=self.env,
        )
        # Update files on disk; both should now be eligible for delta-diff (soft_denies reset to 0)
        f1.write_text("x = 10\n", encoding="utf-8")
        f2.write_text("y = 20\n", encoding="utf-8")
        r1 = read_funnel.check_read(sid, str(f1), cfg=self.cfg, host="codex", env=self.env)
        r2 = read_funnel.check_read(sid, str(f2), cfg=self.cfg, host="codex", env=self.env)
        self.assertIsNotNone(r1)
        self.assertIsNotNone(r2)
        assert r1 is not None and r2 is not None
        self.assertIn("+x = 10", r1["reason"])
        self.assertIn("+y = 20", r2["reason"])

    def test_loop_guard_fail_streak_and_cooldown(self) -> None:
        sid = "sess-loop"
        args = {"CommandLine": "pytest tests/"}
        self.assertIsNone(loop_guard.record_tool_event(sid, "run_command", args, is_error=True, cfg=self.cfg, now=100, env=self.env))
        self.assertIsNone(loop_guard.record_tool_event(sid, "run_command", args, is_error=True, cfg=self.cfg, now=102, env=self.env))
        nudge = loop_guard.record_tool_event(sid, "run_command", args, is_error=True, cfg=self.cfg, now=104, env=self.env)
        self.assertIsNotNone(nudge)
        assert nudge is not None
        self.assertIn("failed 3x", nudge)
        # Pop returns it once then clears
        popped = loop_guard.pop_pending_nudge(sid, cfg=self.cfg, now=105, env=self.env)
        self.assertEqual(popped, nudge)
        self.assertIsNone(loop_guard.pop_pending_nudge(sid, cfg=self.cfg, now=106, env=self.env))

    def test_antigravity_pretooluse_passthrough_is_none(self) -> None:
        # Critical Antigravity contract: passthrough MUST be None (empty stdout), NEVER {}
        payload = {
            "conversationId": "11111111-2222-3333-4444-555555555555",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo hello && rm -f tmp"}},
        }
        res = unified_hook.dispatch("PreToolUse", payload, host_hint="antigravity", env=self.env)
        self.assertIsNone(res)

    def test_claude_posttooluse_compresses_bash_stdout(self) -> None:
        noisy = "\n".join(
            [f"\x1b[33mINFO tick {i}\x1b[0m" for i in range(40)]
            + ["2026-11-24T12:00:01 INFO waiting for server"] * 50
            + ["FAILED test_api.py::test_timeout"]
        )
        payload = {
            "session_id": "claude-s1",
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "tool_response": {"stdout": noisy, "stderr": "", "interrupted": False},
        }
        out = unified_hook.dispatch("PostToolUse", payload, host_hint="claude", env=self.env)
        self.assertIsInstance(out, dict)
        assert isinstance(out, dict)
        hso = out.get("hookSpecificOutput") or {}
        self.assertIn("updatedToolOutput", hso)
        comp_stdout = hso["updatedToolOutput"]["stdout"]
        self.assertLess(len(comp_stdout), len(noisy) * 0.7)
        self.assertIn("FAILED test_api.py::test_timeout", comp_stdout)

    def test_codex_session_start_single_json_envelope(self) -> None:
        payload = {"session_id": "codex-s1", "cwd": str(self.tmp), "source": "startup"}
        out = unified_hook.dispatch("SessionStart", payload, host_hint="codex", env=self.env)
        self.assertIsInstance(out, dict)

    def test_manifests_and_antigravity_keys_strictly_valid(self) -> None:
        allowed_ag = {
            "name", "displayName", "version", "description", "author",
            "homepage", "repository", "suggestedPrompts", "logo", "disabled",
        }
        ag_manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertTrue(set(ag_manifest.keys()).issubset(allowed_ag))
        self.assertEqual(ag_manifest["name"], "terser")

        for rel in (
            "hooks.json",
            "mcp_config.json",
            ".claude-plugin/plugin.json",
            ".claude-plugin/marketplace.json",
            ".codex-plugin/plugin.json",
            "hooks/hooks.json",
            "hooks/codex-hooks.json",
        ):
            data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict, f"{rel} should be a JSON object")

        # Verify Counter front door enforced across all MCP configs
        for rel in ("mcp_config.json", ".claude-plugin/plugin.json"):
            doc = json.loads((ROOT / rel).read_text(encoding="utf-8"))
            env_map = doc["mcpServers"]["terser-index"]["env"]
            self.assertEqual(env_map["TERSER_TOOL_SURFACE"], "counter")

        # The plugin manifest already declares terser-index; a root .mcp.json would register it a
        # second time (and, as a project server, break on the unset ${CLAUDE_PLUGIN_ROOT}).
        self.assertFalse((ROOT / ".mcp.json").exists())

        # Verify AGENTS.md stays lean and includes quality-preserving guardrails
        agents_md = (ROOT / "rules" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertLessEqual(config.estimate_tokens(agents_md), 220)
        self.assertIn("Pragmatic minimalism", agents_md)
        self.assertIn("Never cut validation, error handling, security, or accessibility", agents_md)

    def test_cli_doctor_and_status_exit_zero(self) -> None:
        for cmd in ("doctor", "status"):
            proc = subprocess.run(
                [sys.executable, str(ROOT / "runtime" / "cli.py"), cmd],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(proc.returncode, 0, f"cli {cmd} failed: {proc.stderr}\n{proc.stdout}")

    def test_windows_hook_launcher_smoke(self) -> None:
        if os.name != "nt":
            return
        payload = json.dumps({
            "conversationId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo hi && rm -f x"}},
        })
        proc = subprocess.run(
            ["cmd", "/c", str(ROOT / "scripts" / "terse-hook.cmd"), "--host", "antigravity", "--event", "PreToolUse"],
            input=payload, capture_output=True, text=True, check=False,
            env={**os.environ, "TERSER_HOME": str(self.tmp / "launcher-state")},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Passthrough must emit empty stdout on Antigravity PreToolUse
        self.assertEqual(proc.stdout.strip(), "")


class ResearchUpgradesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.env = {"TERSER_HOME": str(self.tmp / "state")}

    def tearDown(self) -> None:
        self._td.cleanup()

    def test_command_delta_identical_and_small_diff(self) -> None:
        run1 = "\n".join([f"tests/test_mod.py::test_{i} PASSED" for i in range(40)] + ["40 passed in 1.2s"])
        out1, used1 = cmd_delta.apply_command_delta("s-delta", "pytest tests/", run1, returncode=0, now=100, env=self.env)
        self.assertFalse(used1)
        self.assertEqual(out1, run1)

        # Identical repeat -> 1-line receipt
        out2, used2 = cmd_delta.apply_command_delta("s-delta", "pytest tests/", run1, returncode=0, now=120, env=self.env)
        self.assertTrue(used2)
        self.assertIn("Identical output", out2)
        self.assertLess(len(out2), len(run1) * 0.25)

        # Single test changes -> compact unified diff + summary
        run3 = run1.replace("test_12 PASSED", "test_12 FAILED").replace("40 passed", "39 passed, 1 failed")
        out3, used3 = cmd_delta.apply_command_delta("s-delta", "pytest tests/", run3, returncode=1, now=140, env=self.env)
        self.assertTrue(used3)
        self.assertIn("+tests/test_mod.py::test_12 FAILED", out3)
        self.assertIn("exit 0->1", out3)
        self.assertLess(len(out3), len(run3) * 0.5)

    def test_sqlite_fts5_context_sandbox_query_and_ranged_expand(self) -> None:
        lines = [f"INFO line {i}: normal worker heartbeat ok" for i in range(120)]
        lines[77] = "CRITICAL PaymentGatewayTimeoutError: upstream stripe endpoint timed out after 30s"
        raw = "\n".join(lines)
        aid = context_sandbox.store_output(raw, command="pytest", env=self.env)
        self.assertIsNotNone(aid)
        assert aid is not None
        res = context_sandbox.query_output(aid, "PaymentGatewayTimeoutError stripe", env=self.env)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("PaymentGatewayTimeoutError", res)
        self.assertLess(len(res), len(raw) * 0.4)
        rng = context_sandbox.expand_output(aid, start_line=76, end_line=80, env=self.env)
        self.assertIsNotNone(rng)
        assert rng is not None
        self.assertIn("L78: CRITICAL PaymentGatewayTimeoutError", rng)

    def test_subagent_start_and_runaway_glob_guard(self) -> None:
        # SubagentStart injects compact economy brief on Claude & Codex
        for h in ("claude", "codex"):
            out = unified_hook.dispatch("SubagentStart", {"session_id": f"{h}-sub"}, host_hint=h, env=self.env)
            assert isinstance(out, dict)
            ctx = (out.get("hookSpecificOutput") or {}).get("additionalContext") or ""
            self.assertIn("Terser Subagent", ctx)

        # Runaway Glob (>8KB and >120 lines) is capped + FTS5 archived
        huge_glob = "\n".join(f"src/deep/module_{i}/component_{i}.tsx" for i in range(300))
        out = unified_hook.dispatch(
            "PostToolUse",
            {"session_id": "claude-g1", "tool_name": "Glob", "tool_input": {"pattern": "**/*"}, "tool_response": huge_glob},
            host_hint="claude",
            env=self.env,
        )
        assert isinstance(out, dict)
        capped = (out.get("hookSpecificOutput") or {}).get("updatedToolOutput") or ""
        self.assertIn("more Glob lines omitted", capped)
        self.assertIn('order(action="search_sandbox"', capped)
        self.assertLess(len(capped), len(huge_glob) * 0.4)

    def test_smart_crusher_preserves_errors_outliers_and_factors_constants(self) -> None:
        items = [
            {"id": i, "org": "acme", "region": "us-east-1", "status": "ok", "latency_ms": 20 + (i % 3)}
            for i in range(100)
        ]
        items[35]["status"] = "failed"
        items[35]["error"] = "db_timeout"
        items[72]["latency_ms"] = 9999  # clear statistical outlier
        raw = json.dumps({"total": 100, "items": items}, indent=2)
        context_sandbox.set_hint_host("claude")
        self.addCleanup(context_sandbox.set_hint_host, None)
        crushed = smart_crusher.crush_json(raw, command="mcp_list", env=self.env)
        self.assertIsNotNone(crushed)
        assert crushed is not None
        self.assertLess(len(crushed), len(raw) * 0.25)
        parsed = json.loads(crushed)
        inner = parsed["items"]
        self.assertEqual(inner["_const"]["org"], "acme")
        self.assertEqual(inner["_const"]["region"], "us-east-1")
        self.assertIn(35, inner["_kept_indices"])
        self.assertIn(72, inner["_kept_indices"])
        self.assertIn('order(action="search_sandbox"', inner["_retrieve"])

    def test_lockfile_diff_elision_and_framework_stack_folding(self) -> None:
        diff = (
            "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
            "@@ -1 +1 @@\n-old()\n+new()\n"
            "diff --git a/package-lock.json b/package-lock.json\n"
            + "".join(f'+  "pkg_{i}": "1.0.0",\n' for i in range(50))
        )
        out = output_compressor.elide_lockfile_diffs(diff)
        self.assertIn("+new()", out)
        self.assertIn("elided lockfile/generated diff", out)
        self.assertNotIn("pkg_49", out)

        tb = (
            "Traceback (most recent call last):\n"
            '  File "src/app.py", line 12, in main\n    run()\n'
            '  File "/usr/lib/python3.12/site-packages/starlette/routing.py", line 1, in f1\n    x()\n'
            '  File "/usr/lib/python3.12/site-packages/starlette/routing.py", line 2, in f2\n    y()\n'
            '  File "/usr/lib/python3.12/site-packages/starlette/routing.py", line 3, in f3\n    z()\n'
            '  File "/usr/lib/python3.12/site-packages/starlette/routing.py", line 4, in f4\n    w()\n'
            '  File "src/handler.py", line 44, in handle\n    raise KeyError("user_id")\n'
            "KeyError: 'user_id'\n"
        )
        folded = output_compressor.fold_framework_stack_frames(tb)
        self.assertIn("4 framework/stdlib stack frames omitted", folded)
        self.assertIn('File "src/app.py"', folded)
        self.assertIn('File "src/handler.py"', folded)
        self.assertIn("KeyError: 'user_id'", folded)

    def test_config_auditor_and_task_router(self) -> None:
        ws = self.tmp / "audit_ws"
        (ws / ".claude").mkdir(parents=True)
        (ws / "CLAUDE.md").write_text("rule line\n" * 1000, encoding="utf-8")
        (ws / ".claude" / "settings.local.json").write_text('{"cmd": "curl -H Bearer sk-1234567890123456789012345"}', encoding="utf-8")
        rep = config_auditor.audit_workspace(str(ws), home=self.tmp)
        self.assertGreater(rep["total_static_tokens"], 1500)
        joined = "\n".join(rep["warnings"])
        self.assertIn("SECURITY", joined)
        self.assertIn(".gitignore", joined)

        low = task_router.classify_task("Fix typo in README")
        high = task_router.classify_task("Architect a distributed lock-free state machine and debug race condition across 10 files")
        self.assertEqual(low["tier"], "low")
        self.assertEqual(high["tier"], "high")

    def test_pagerank_repo_map_ranks_central_module_and_is_byte_stable(self) -> None:
        repo = self.tmp / "sample_repo"
        repo.mkdir()
        (repo / "core.py").write_text("class Engine:\n    pass\ndef run_engine():\n    pass\n", encoding="utf-8")
        for name in ("api.py", "worker.py", "cli.py", "admin.py", "scheduler.py"):
            (repo / name).write_text("from core import Engine, run_engine\ndef start():\n    return run_engine()\n", encoding="utf-8")
        rmap1 = repo_map.build_repo_map(str(repo), max_tokens=200, min_files=3, env=self.env)
        rmap2 = repo_map.build_repo_map(str(repo), max_tokens=200, min_files=3, env=self.env)
        self.assertIsNotNone(rmap1)
        assert rmap1 is not None
        self.assertEqual(rmap1, rmap2)  # 100% byte-stable for prompt caching!
        # core.py is imported by all 5 modules, so it must rank first
        first_entry = rmap1.splitlines()[1]
        self.assertIn("core.py", first_entry)


class NativeFastPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="terser-native-test-"))
        self.env = {**os.environ, "TERSER_HOME": str(self.tmp / "state")}
        self.fast_bin = ROOT / "bin" / ("terser-fast.exe" if os.name == "nt" else "terser-fast")

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_fast(self, event: str, payload: dict, *, host: str = "antigravity", with_py: bool = True) -> subprocess.CompletedProcess:
        cmd = [str(self.fast_bin), "--host", host, "--event", event]
        if with_py:
            cmd.extend(["--python", sys.executable, "--script", str(ROOT / "runtime" / "unified_hook.py")])
        return subprocess.run(
            cmd,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            timeout=15,
        )

    def test_native_fast_path_read_funnel_mcp_loop_guard_and_delegation(self) -> None:
        if not self.fast_bin.is_file():
            self.skipTest("Optional Rust binary bin/terser-fast not built")

        ver = subprocess.run([str(self.fast_bin), "--version"], capture_output=True, text=True, timeout=5)
        self.assertEqual(ver.returncode, 0)
        self.assertIn("terser-fast", ver.stdout)

        # 1. Tier 3 first read -> silent passthrough; Tier 0 repeat -> deny; retry -> passthrough
        sample = self.tmp / "small.py"
        sample.write_text("x = 1\ny = 2\n", encoding="utf-8")
        read_payload = {
            "conversationId": "conv-native-1",
            "toolCall": {"name": "read_file", "args": {"Path": str(sample)}},
        }
        p1 = self._run_fast("PreToolUse", read_payload, with_py=False)
        self.assertEqual(p1.returncode, 0)
        self.assertEqual(p1.stdout.strip(), "")

        p2 = self._run_fast("PreToolUse", read_payload, with_py=False)
        self.assertEqual(p2.returncode, 0)
        d2 = json.loads(p2.stdout)
        self.assertEqual(d2["decision"], "deny")
        self.assertIn("unchanged", d2["reason"])

        p3 = self._run_fast("PreToolUse", read_payload, with_py=False)
        self.assertEqual(p3.returncode, 0)
        self.assertEqual(p3.stdout.strip(), "")

        # 2. MCP read-only refetch guard handled natively
        mcp_payload = {
            "conversationId": "conv-native-1",
            "toolCall": {
                "name": "call_mcp_tool",
                "args": {"ServerName": "terser-index", "ToolName": "search_symbols", "Arguments": {"q": "foo"}},
            },
        }
        m1 = self._run_fast("PreToolUse", mcp_payload, with_py=False)
        self.assertEqual(m1.returncode, 0)
        self.assertEqual(m1.stdout.strip(), "")
        m2 = self._run_fast("PreToolUse", mcp_payload, with_py=False)
        self.assertEqual(m2.returncode, 0)
        self.assertEqual(json.loads(m2.stdout)["decision"], "deny")

        # 3. SubagentStart handled natively
        sub = self._run_fast("SubagentStart", {"session_id": "s-sub"}, host="claude", with_py=False)
        self.assertEqual(sub.returncode, 0)
        self.assertIn("Terser Subagent", sub.stdout)

        # 4. Loop guard on PostToolUse (3 identical failures -> nudge)
        fail_payload = {
            "session_id": "s-loop-native",
            "tool_name": "Read",
            "tool_input": {"file_path": str(sample)},
            "error": "boom",
        }
        self._run_fast("PostToolUse", fail_payload, host="claude", with_py=False)
        self._run_fast("PostToolUse", fail_payload, host="claude", with_py=False)
        l3 = self._run_fast("PostToolUse", fail_payload, host="claude", with_py=False)
        self.assertEqual(l3.returncode, 0)
        self.assertIn("failed 3x in a row", l3.stdout)

        # 5. Delegation to Python when needed (e.g. cold-path SessionStart)
        cold = self._run_fast("SessionStart", {"conversationId": "conv-cold"}, host="antigravity", with_py=True)
        self.assertEqual(cold.returncode, 0)

        # 6. Native `compress` subcommand (Cargo stream filter + ANSI strip + error preservation)
        noisy = "\x1b[32m   Compiling a v0.1\x1b[0m\n   Compiling b v0.1\n   Compiling c v0.1\nerror[E0425]: cannot find value\n"
        comp = subprocess.run(
            [str(self.fast_bin), "compress", "--cmd", "cargo build"],
            input=noisy,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
        )
        self.assertEqual(comp.returncode, 0)
        self.assertIn("error[E0425]", comp.stdout)
        self.assertNotIn("Compiling a", comp.stdout)

        # 7. Native `exec` subcommand (preserves exit code & tee-on-failure)
        ok_exec = subprocess.run(
            [str(self.fast_bin), "exec", "--", sys.executable, "-c", "print('hello native exec')"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            timeout=10,
        )
        self.assertEqual(ok_exec.returncode, 0)
        self.assertIn("hello native exec", ok_exec.stdout)

        fail_exec = subprocess.run(
            [str(self.fast_bin), "exec", "--", sys.executable, "-c", "import sys; print('RAW_FAIL'); sys.exit(7)"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            timeout=10,
        )
        self.assertEqual(fail_exec.returncode, 7)
        self.assertIn("RAW_FAIL", fail_exec.stdout)

    def test_sandbox_last_shortcut_and_size_pruning(self) -> None:
        aid1 = context_sandbox.store_output("alpha line\n" * 40, command="cmd1", env=self.env)
        aid2 = context_sandbox.store_output("beta unique_needle\n" * 40, command="cmd2", env=self.env)
        self.assertIsNotNone(aid1)
        self.assertIsNotNone(aid2)
        self.assertEqual(context_sandbox.get_latest_archive_id(env=self.env), aid2)
        res = context_sandbox.query_output("--last", "unique_needle", env=self.env)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("unique_needle", res)
        exp = context_sandbox.expand_output("--last", start_line=1, end_line=2, env=self.env)
        self.assertIsNotNone(exp)
        assert exp is not None
        self.assertIn("L1: beta unique_needle", exp)

    def test_builtin_terser_index_mcp_server(self) -> None:
        from runtime import index_mcp

        ws = self.tmp / "indexed_ws"
        ws.mkdir()
        (ws / "calc.py").write_text(
            "class Calculator:\n    def add_numbers(self, a, b):\n        return a + b\n",
            encoding="utf-8",
        )
        res = index_mcp.index_folder(str(ws), env=self.env)
        self.assertEqual(res["files"], 1)
        self.assertGreaterEqual(res["symbols"], 2)
        repo = res["repo"]

        found = index_mcp.dispatch_action("search_symbols", {"repo": repo, "query": "add_numbers"}, env=self.env)
        self.assertTrue(found["matches"])
        sym_id = found["matches"][0]["id"]
        src = index_mcp.dispatch_action("get_symbol_source", {"repo": repo, "symbol_id": sym_id}, env=self.env)
        self.assertIn("return a + b", src["source"])

    def test_windows_no_window_spawn_helpers(self) -> None:
        from runtime import config as cfg_mod

        nw = cfg_mod.no_window_kwargs()
        det = cfg_mod.detached_spawn_kwargs(breakaway=False)
        det_ba = cfg_mod.detached_spawn_kwargs(breakaway=True)
        if sys.platform == "win32":
            create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            detached_process = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
            breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)

            self.assertEqual(nw.get("creationflags"), create_no_window)
            self.assertTrue(det["creationflags"] & create_no_window)
            self.assertTrue(det["creationflags"] & new_group)
            # DETACHED_PROCESS overrides CREATE_NO_WINDOW on Windows and causes child console flashes
            self.assertFalse(det["creationflags"] & detached_process)
            self.assertFalse(det_ba["creationflags"] & detached_process)
            self.assertTrue(det_ba["creationflags"] & breakaway)
        else:
            self.assertEqual(nw, {})
            self.assertEqual(det, {"start_new_session": True})

    def test_sandbox_redaction_porter_bm25_and_chunking(self) -> None:
        # 1. Credential redaction before storing to sandbox.db
        secret = "ghp_1234567890abcdefghijklmnopqrstuvwx"
        raw = f"# Auth Setup\nToken: {secret}\n\n## Build Notes\n```python\ndef compiled_worker():\n    return 42\n```\n"
        aid = context_sandbox.store_output(raw, command=f"curl -H {secret}", env=self.env)
        self.assertIsNotNone(aid)
        assert aid is not None
        expanded = context_sandbox.expand_output(aid, env=self.env) or ""
        self.assertNotIn(secret, expanded)

        # 2. Porter stemming: querying "compiling" matches "compiled_worker" / "compiled"
        raw2 = "# Section One\nWe compiled the module cleanly.\n\n# Section Two\nUnrelated filler text.\n"
        aid2 = context_sandbox.store_output(raw2, command="build", env=self.env)
        assert aid2 is not None
        res = context_sandbox.query_output(aid2, "compiling", env=self.env) or ""
        self.assertIn("compiled", res)

        # 3. Structure-aware chunking keeps fenced code blocks intact
        lines = ["# Heading", "Intro line", "```py"] + [f"x = {i}" for i in range(20)] + ["```"]
        chunks = context_sandbox._chunk_lines(lines)
        self.assertTrue(any("```py" in b and "x = 19" in b and b.rstrip().endswith("```") for _, _, b in chunks))

    def test_mcp_sandbox_actions_and_execute_filtered(self) -> None:
        from runtime import index_mcp

        # index_sandbox_file + search_sandbox + expand_sandbox
        doc = self.tmp / "guide.md"
        doc.write_text("# Overview\nAlpha intro.\n\n# Troubleshooting\nFix needle_err_99 by restarting.\n", encoding="utf-8")
        idx_res = index_mcp.dispatch_action("index_sandbox_file", {"path": str(doc)}, env=self.env)
        self.assertIn("archive_id", idx_res)
        aid = idx_res["archive_id"]

        s_res = index_mcp.dispatch_action("search_sandbox", {"archive_id": aid, "query": "needle_err_99"}, env=self.env)
        self.assertIn("needle_err_99", s_res.get("result", ""))

        e_res = index_mcp.dispatch_action("expand_sandbox", {"archive_id": aid, "start_line": 1, "end_line": 2}, env=self.env)
        self.assertIn("Overview", e_res.get("content", ""))

        # execute_filtered: >5KB stdout with intent -> filtered via FTS5
        py_script = "for i in range(220): print(f'log line {i}: ' + ('CRITICAL_TARGET_XYZ' if i == 150 else 'routine noise padding ' * 2))"
        ex_res = index_mcp.dispatch_action(
            "execute_filtered",
            {"argv": [sys.executable, "-c", py_script], "intent": "CRITICAL_TARGET_XYZ"},
            env=self.env,
        )
        self.assertEqual(ex_res.get("returncode"), 0)
        self.assertTrue(ex_res.get("filtered"))
        self.assertIn("CRITICAL_TARGET_XYZ", ex_res.get("output", ""))
        self.assertTrue(ex_res.get("archive_id"))

    def test_live_session_event_continuity_and_shell_edit_progress(self) -> None:
        ws = str(self.tmp / "cont_ws")
        Path(ws).mkdir()
        context_sandbox.record_session_event("s-old", ws, "edit", "engine.py", env=self.env)
        context_sandbox.record_session_event("s-old", ws, "error", "pytest: AssertionError", env=self.env)

        from runtime.config import DEFAULT_CONFIG

        cfg = dict(DEFAULT_CONFIG)
        brief, notice = session_brief.build_session_brief(ws, host="antigravity", cfg=cfg, env=self.env)
        self.assertIsNotNone(brief)
        assert brief is not None
        self.assertIn("engine.py", brief)
        self.assertIn("AssertionError", brief)

        # Shell-edit progress resets failure streaks in loop_guard
        sid = "s-shell-edit"
        loop_guard.record_tool_event(sid, "run_command", {"CommandLine": "pytest"}, is_error=True, cfg=cfg, env=self.env)
        loop_guard.record_tool_event(sid, "run_command", {"CommandLine": "pytest"}, is_error=True, cfg=cfg, env=self.env)
        # Successful shell edit clears fail streak
        loop_guard.record_tool_event(sid, "run_command", {"CommandLine": "sed -i 's/a/b/' app.py"}, is_error=False, cfg=cfg, env=self.env)
        nudge = loop_guard.record_tool_event(sid, "run_command", {"CommandLine": "pytest"}, is_error=True, cfg=cfg, env=self.env)
        self.assertIsNone(nudge)

    def test_subagent_support_across_antigravity_claude_and_codex(self) -> None:
        # 1. Antigravity 2.0: PreToolUse injects brief into invoke_subagent, swarm add, define_subagent
        ag_inv = unified_hook.dispatch(
            "PreToolUse",
            {
                "conversationId": "ag-parent-1",
                "toolCall": {
                    "name": "invoke_subagent",
                    "args": {"TypeName": "research", "Role": "Code Researcher", "Prompt": "Find auth handler."},
                },
            },
            host_hint="antigravity",
            env=self.env,
        )
        assert isinstance(ag_inv, dict)
        self.assertEqual(ag_inv["decision"], "allow")
        self.assertIn("[Terser Subagent]", ag_inv["overwrite"]["Prompt"])
        self.assertIn("StartLine`/`EndLine", ag_inv["overwrite"]["Prompt"])

        # Idempotent: already-injected prompt passes through as None
        ag_inv_dup = unified_hook.dispatch(
            "PreToolUse",
            {
                "conversationId": "ag-parent-1",
                "toolCall": {
                    "name": "invoke_subagent",
                    "args": {"TypeName": "research", "Prompt": ag_inv["overwrite"]["Prompt"]},
                },
            },
            host_hint="antigravity",
            env=self.env,
        )
        self.assertIsNone(ag_inv_dup)

        # Browser and image-generator subagents are skipped (passthrough None)
        for skip_type in ("browser", "image-generator"):
            res_skip = unified_hook.dispatch(
                "PreToolUse",
                {
                    "conversationId": "ag-parent-1",
                    "toolCall": {"name": "invoke_subagent", "args": {"TypeName": skip_type, "Prompt": "Open page"}},
                },
                host_hint="antigravity",
                env=self.env,
            )
            self.assertIsNone(res_skip)

        # Swarm add injects into brief; swarm status passes through
        ag_swarm = unified_hook.dispatch(
            "PreToolUse",
            {
                "conversationId": "ag-parent-1",
                "toolCall": {"name": "swarm", "args": {"action": "add", "name": "worker", "brief": "Audit tests"}},
            },
            host_hint="antigravity",
            env=self.env,
        )
        assert isinstance(ag_swarm, dict)
        self.assertIn("[Terser Subagent]", ag_swarm["overwrite"]["brief"])
        self.assertIsNone(
            unified_hook.dispatch(
                "PreToolUse",
                {"conversationId": "ag-parent-1", "toolCall": {"name": "swarm", "args": {"action": "status"}}},
                host_hint="antigravity",
                env=self.env,
            )
        )

        # 2. Claude Read Funnel stays scoped per subagent. Codex Read needs delivered evidence.
        sample = self.tmp / "shared_mod.py"
        for host in ("claude", "codex"):
            sample.write_text(f"value = '{host}_1'\n", encoding="utf-8")
            sid = f"{host}-parent-42"
            # Parent reads sample -> allowed
            self.assertIsNone(
                unified_hook.dispatch(
                    "PreToolUse",
                    {"session_id": sid, "tool_name": "Read", "tool_input": {"file_path": str(sample)}},
                    host_hint=host,
                    env=self.env,
                )
            )
            # Parent re-reads sample -> soft-denied
            p_dup = unified_hook.dispatch(
                "PreToolUse",
                {"session_id": sid, "tool_name": "Read", "tool_input": {"file_path": str(sample)}},
                host_hint=host,
                env=self.env,
            )
            if host == "claude":
                self.assertEqual(p_dup["hookSpecificOutput"]["permissionDecision"], "deny")
            else:
                self.assertIsNone(p_dup)

            # Subagent A has a fresh context -> first read of sample MUST pass through!
            self.assertIsNone(
                unified_hook.dispatch(
                    "PreToolUse",
                    {"session_id": sid, "agent_id": "sub-a1", "tool_name": "Read", "tool_input": {"file_path": str(sample)}},
                    host_hint=host,
                    env=self.env,
                )
            )
            # Subagent A's second unchanged read -> soft-denied within its own scope!
            sub_dup = unified_hook.dispatch(
                "PreToolUse",
                {"session_id": sid, "agent_id": "sub-a1", "tool_name": "Read", "tool_input": {"file_path": str(sample)}},
                host_hint=host,
                env=self.env,
            )
            if host == "claude":
                self.assertEqual(sub_dup["hookSpecificOutput"]["permissionDecision"], "deny")
            else:
                self.assertIsNone(sub_dup)

            # Subagent A edits sample -> invalidates parent's soft_denies too!
            unified_hook.dispatch(
                "PostToolUse",
                {"session_id": sid, "agent_id": "sub-a1", "tool_name": "Edit", "tool_input": {"file_path": str(sample)}},
                host_hint=host,
                env=self.env,
            )
            sample.write_text(f"value = '{host}_2'\n", encoding="utf-8")
            p_after = unified_hook.dispatch(
                "PreToolUse",
                {"session_id": sid, "tool_name": "Read", "tool_input": {"file_path": str(sample)}},
                host_hint=host,
                env=self.env,
            )
            if host == "claude":
                self.assertIn(f"+value = '{host}_2'", p_after["hookSpecificOutput"]["permissionDecisionReason"])
            else:
                self.assertIsNone(p_after)

            # SubagentStop removes subagent ephemeral cache without deleting parent cache
            unified_hook.dispatch(
                "SubagentStop",
                {"session_id": sid, "agent_id": "sub-a1"},
                host_hint=host,
                env=self.env,
            )
            rc_dir = Path(self.env["TERSER_HOME"]) / "read-cache"
            self.assertFalse((rc_dir / f"{sid}--sub-a1.json").exists())
            self.assertEqual((rc_dir / f"{sid}.json").exists(), host == "claude")

        # 3. Native fast-path parity for AGY invoke_subagent & Claude agent_id isolation
        if self.fast_bin.is_file():
            nat_ag = self._run_fast(
                "PreToolUse",
                {
                    "conversationId": "ag-nat-1",
                    "toolCall": {"name": "invoke_subagent", "args": {"TypeName": "self", "Prompt": "Inspect API"}},
                },
                host="antigravity",
                with_py=False,
            )
            self.assertEqual(nat_ag.returncode, 0)
            nat_doc = json.loads(nat_ag.stdout)
            self.assertEqual(nat_doc["decision"], "allow")
            self.assertIn("[Terser Subagent]", nat_doc["overwrite"]["Prompt"])

            # Native Claude agent_id isolation + SubagentStop cleanup
            f_nat = self.tmp / "nat_shared.py"
            f_nat.write_text("a = 1\n", encoding="utf-8")
            self._run_fast("PreToolUse", {"session_id": "nat-p1", "tool_name": "Read", "tool_input": {"file_path": str(f_nat)}}, host="claude", with_py=False)
            sub_first = self._run_fast(
                "PreToolUse",
                {"session_id": "nat-p1", "agent_id": "agent-99", "tool_name": "Read", "tool_input": {"file_path": str(f_nat)}},
                host="claude",
                with_py=False,
            )
            self.assertEqual(sub_first.stdout.strip(), "")
            self._run_fast("SubagentStop", {"session_id": "nat-p1", "agent_id": "agent-99"}, host="claude", with_py=False)
            self.assertFalse((Path(self.env["TERSER_HOME"]) / "read-cache" / "nat-p1--agent-99.json").exists())


class TokenEfficiencyUpgradesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.env = dict(os.environ, TERSER_HOME=str(self.tmp / "terser_home"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_index_mcp_exact_brace_end_lines_and_compact_formatting(self) -> None:
        from runtime import index_mcp

        ws = self.tmp / "ts_ws"
        ws.mkdir()
        body_lines = [f"  const v{i} = {i}; // comment with }} brace" for i in range(40)]
        ts_src = "export function bigHandler(x: number) {\n" + "\n".join(body_lines) + "\n  return v39;\n}\n\nexport function tiny() {\n  return 1;\n}\n"
        (ws / "app.ts").write_text(ts_src, encoding="utf-8")

        res = index_mcp.index_folder(str(ws), env=self.env)
        repo = res["repo"]
        found = index_mcp.dispatch_action("search_symbols", {"repo": repo, "query": "bigHandler"}, env=self.env)
        sym = found["matches"][0]
        # Must capture full 43-line function instead of truncating at +25
        self.assertEqual(sym["start_line"], 1)
        self.assertEqual(sym["end_line"], 43)

        tiny = index_mcp.dispatch_action("search_symbols", {"repo": repo, "query": "tiny"}, env=self.env)["matches"][0]
        self.assertEqual(tiny["end_line"] - tiny["start_line"], 2)

        # Compact MCP formatting must be significantly smaller than indented JSON
        indented = json.dumps(found, indent=2)
        compact = index_mcp.format_mcp_response("search_symbols", found)
        self.assertLess(len(compact), len(indented) * 0.65)
        self.assertIn("bigHandler", compact)

    def test_read_funnel_tier2c_data_lockfiles_logs_disjoint_intervals_and_write_seed(self) -> None:
        from runtime.config import DEFAULT_CONFIG

        cfg = dict(DEFAULT_CONFIG, outline_min_bytes=500, outline_min_lines=10)

        # 1. Large JSON -> Tier 2c key/schema outline
        jf = self.tmp / "openapi.json"
        jf.write_text(
            json.dumps({"openapi": "3.1.0", "info": {"title": "API", "v": "1"}, "paths": {f"/p{i}": {"get": i} for i in range(40)}}, indent=2),
            encoding="utf-8",
        )
        r_json = read_funnel.check_read("s-t2c", str(jf), cfg=cfg, env=self.env)
        self.assertIsNotNone(r_json)
        self.assertIn("paths: object{40 keys}", r_json["reason"])

        # 2. Lockfile -> Tier 2c soft-deny even if single-line minified
        lf = self.tmp / "package-lock.json"
        lf.write_text("{" + '"a":1,' * 200 + '"z":2}', encoding="utf-8")
        r_lock = read_funnel.check_read("s-t2c", str(lf), cfg=cfg, env=self.env)
        self.assertIsNotNone(r_lock)
        self.assertIn("Lockfile/generated", r_lock["reason"])

        # 3. Large log -> auto-indexed in sandbox.db with signal lines preserved
        logf = self.tmp / "server.log"
        log_lines = [f"2026-11-26 10:00:{i:02d} INFO ok line {i} padding padding" for i in range(40)]
        log_lines[20] = "2026-11-26 10:00:20 ERROR database connection refused"
        logf.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        context_sandbox.set_hint_host("claude")
        self.addCleanup(context_sandbox.set_hint_host, None)
        r_log = read_funnel.check_read("s-t2c", str(logf), cfg=cfg, env=self.env)
        self.assertIsNotNone(r_log)
        self.assertIn("database connection refused", r_log["reason"])
        self.assertIn('order(action="search_sandbox"', r_log["reason"])

        # 4. Disjoint intervals: reading 1-10 then 30-40 must NOT claim 15-25 is covered
        codef = self.tmp / "ranges.py"
        codef.write_text("\n".join(f"x_{i} = {i}" for i in range(50)) + "\n", encoding="utf-8")
        self.assertIsNone(read_funnel.check_read("s-iv", str(codef), start_line=1, end_line=10, cfg=cfg, env=self.env))
        self.assertIsNone(read_funnel.check_read("s-iv", str(codef), start_line=30, end_line=40, cfg=cfg, env=self.env))
        # Unread middle gap MUST pass through (None)!
        self.assertIsNone(read_funnel.check_read("s-iv", str(codef), start_line=15, end_line=25, cfg=cfg, env=self.env))
        # Already-read subrange MUST hit T0 dedup!
        dup = read_funnel.check_read("s-iv", str(codef), start_line=2, end_line=8, cfg=cfg, env=self.env)
        self.assertIsNotNone(dup)
        self.assertEqual(dup["action"], "deny")

        # 5. seed_after_write -> immediate verification re-read hits T0 dedup
        wf = self.tmp / "written.py"
        wf.write_text("def fresh():\n    return 42\n", encoding="utf-8")
        read_funnel.seed_after_write("s-wr", str(wf), env=self.env)
        r_wr = read_funnel.check_read("s-wr", str(wf), cfg=cfg, env=self.env)
        self.assertIsNotNone(r_wr)
        self.assertEqual(r_wr["action"], "deny")

    def test_output_compressor_path_elision_grep_grouping_and_new_stream_filters(self) -> None:
        ws = "/home/developer/projects/monorepo/packages/core"
        raw_paths = "\n".join(f"{ws}/src/mod_{i}.ts:{i}:1 error TS2322: mismatch" for i in range(12))
        elided = output_compressor.elide_workspace_paths(raw_paths, workspace=ws)
        self.assertTrue(elided.startswith(f"[root: {ws}]"))
        self.assertIn("./src/mod_0.ts", elided)
        self.assertLess(len(elided), len(raw_paths))

        raw_grep = "\n".join(
            [f"packages/core/src/engine/processor.py:{i}: match_value = {i}" for i in range(10, 20)]
            + [f"packages/core/src/engine/helper.py:{i}: match_value = {i}" for i in range(1, 6)]
        )
        grouped = output_compressor.group_grep_output(raw_grep)
        self.assertIn("packages/core/src/engine/processor.py:\n  10:", grouped)
        self.assertLess(len(grouped), len(raw_grep) * 0.6)

        pytest_v = "\n".join(
            [f"tests/test_app.py::test_case_{i} PASSED [ {i*5}%]" for i in range(1, 12)]
            + ["tests/test_app.py::test_broken FAILED", "AssertionError: expected 1 == 2", "1 failed, 11 passed in 0.42s"]
        )
        filtered = output_compressor.apply_stream_filters(pytest_v, "pytest -v")
        self.assertIn("test_broken FAILED", filtered)
        self.assertIn("AssertionError", filtered)
        self.assertIn("1 failed, 11 passed", filtered)
        self.assertNotIn("test_case_5 PASSED", filtered)

    def test_smart_crusher_ndjson_and_nested_graphql_and_cmd_delta_timing_norm(self) -> None:
        # NDJSON
        nd_lines = [
            json.dumps({"service": "api", "env": "prod", "id": i, "latency_ms": 12 + (i % 3), "status": "ok"})
            for i in range(20)
        ]
        nd_text = "\n".join(nd_lines)
        crushed_nd = smart_crusher.crush_json(nd_text, command="kubectl logs", env=self.env)
        self.assertIsNotNone(crushed_nd)
        parsed_nd = json.loads(crushed_nd)
        self.assertEqual(parsed_nd.get("_format"), "columnar")
        self.assertEqual(parsed_nd.get("_source"), "ndjson")
        self.assertLess(len(crushed_nd), len(nd_text) * 0.65)

        # Nested GraphQL envelope (data.repository.issues.nodes)
        gql = json.dumps({
            "data": {
                "repository": {
                    "name": "terser",
                    "issues": {
                        "nodes": [
                            {"state": "OPEN", "author": "alice", "num": i, "title": f"Issue number {i} description text"}
                            for i in range(15)
                        ]
                    },
                }
            }
        })
        crushed_gql = smart_crusher.crush_json(gql, command="gh api graphql", env=self.env)
        self.assertIsNotNone(crushed_gql)
        parsed_gql = json.loads(crushed_gql)
        self.assertEqual(parsed_gql["data"]["repository"]["issues"]["nodes"]["_format"], "columnar")

        # cmd_delta timing-only-varying output -> 1-line identical receipt!
        base = "\n".join(f"test_{i} ok padding padding" for i in range(20))
        out1 = base + "\n20 passed in 1.42s"
        out2 = base + "\n20 passed in 1.89s"
        _, used1 = cmd_delta.apply_command_delta("s-time", "pytest", out1, returncode=0, env=self.env)
        res2, used2 = cmd_delta.apply_command_delta("s-time", "pytest", out2, returncode=0, env=self.env)
        self.assertFalse(used1)
        self.assertTrue(used2)
        self.assertIn("Identical output", res2)
        self.assertIn("1.89s", res2)

    def test_and_chain_wrapping_subagent_routing_and_codex_grep_passthrough(self) -> None:
        from runtime.config import DEFAULT_CONFIG

        # 1. Safe &&-chain wrapping
        wrapped, _ = unified_hook._wrap_command_for_compression("git status && git diff", DEFAULT_CONFIG, session_id="s-ch")
        self.assertIsNotNone(wrapped)
        self.assertIn(" && ", wrapped)

        # 2. Subagent complexity routing (opt-in): low-complexity research -> Model: flash
        ag_payload = {
            "conversationId": "ag-route-1",
            "toolCall": {
                "name": "invoke_subagent",
                "args": {"TypeName": "research", "Prompt": "Find where typo in readme is"},
            },
        }
        default_route = unified_hook.dispatch("PreToolUse", ag_payload, host_hint="antigravity", env=self.env)
        self.assertNotIn("Model", ((default_route or {}).get("overwrite") or {}))
        routed = unified_hook.dispatch(
            "PreToolUse",
            ag_payload,
            host_hint="antigravity",
            env=dict(self.env, TERSER_SUBAGENT_ROUTING="1"),
        )
        assert isinstance(routed, dict)
        self.assertEqual(routed["overwrite"].get("Model"), "flash")

        # Explicit Model="pro" must NEVER be downgraded
        explicit = unified_hook.dispatch(
            "PreToolUse",
            {
                "conversationId": "ag-route-1",
                "toolCall": {
                    "name": "invoke_subagent",
                    "args": {"TypeName": "research", "Model": "pro", "Prompt": "Find where typo in readme is"},
                },
            },
            host_hint="antigravity",
            env=self.env,
        )
        assert isinstance(explicit, dict)
        self.assertNotIn("Model", explicit["overwrite"])

        # 3. Codex PostToolUse does not support replacing the tool response.
        raw_grep = "\n".join(f"src/very/deep/module/handler.py:{i}: const token = {i};" for i in range(1, 15))
        cx = unified_hook.dispatch(
            "PostToolUse",
            {"session_id": "cx-grep-1", "tool_name": "Grep", "tool_input": {"pattern": "token"}, "tool_response": raw_grep},
            host_hint="codex",
            env=self.env,
        )
        hso = (cx or {}).get("hookSpecificOutput") or {}
        self.assertNotIn("updatedToolOutput", hso)
        self.assertNotIn("updatedMCPToolOutput", hso)

    def test_compiler_note_dedup_claude_agent_routing_and_native_exec_delta(self) -> None:
        from runtime import index_mcp

        # 1. Repeated rustc/clippy note/help boilerplate deduplication
        diag = "\n".join([
            "error[E0308]: mismatched types in src/a.rs:10",
            "  = note: for more information about this error, try `rustc --explain E0308`",
            "error[E0308]: mismatched types in src/b.rs:22",
            "  = note: for more information about this error, try `rustc --explain E0308`",
            "error[E0308]: mismatched types in src/c.rs:35",
            "  = note: for more information about this error, try `rustc --explain E0308`",
        ])
        deduped = output_compressor.dedup_compiler_notes(diag)
        self.assertEqual(deduped.count("rustc --explain E0308"), 1)
        self.assertIn("src/a.rs:10", deduped)
        self.assertIn("src/b.rs:22", deduped)
        self.assertIn("src/c.rs:35", deduped)

        # 2. Compact MCP source block formatting for assemble_task_context (no JSON newline escaping)
        formatted = index_mcp.format_mcp_response(
            "assemble_task_context",
            {
                "repo": "demo",
                "approx_tokens": 50,
                "symbols": [{"id": "a.py::fn", "file": "a.py", "start_line": 1, "end_line": 2, "source": "1: def fn():\n2:     return 1"}],
            },
        )
        self.assertIn("--- a.py::fn (a.py L1-2) ---\n1: def fn():\n2:     return 1", formatted)

        # 3. Claude Code Agent routing is opt-in (it trades quality for cost): default leaves the model alone
        agent_payload = {
            "session_id": "cl-ag-1",
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "Explore", "prompt": "Find where typo in readme is"},
        }
        cl_default = unified_hook.dispatch("PreToolUse", agent_payload, host_hint="claude", env=self.env)
        hso = (cl_default or {}).get("hookSpecificOutput") or {}
        self.assertNotIn("model", hso.get("updatedInput") or {})
        cl_route = unified_hook.dispatch(
            "PreToolUse",
            agent_payload,
            host_hint="claude",
            env=dict(self.env, TERSER_SUBAGENT_ROUTING="1"),
        )
        hso = (cl_route or {}).get("hookSpecificOutput") or {}
        self.assertEqual((hso.get("updatedInput") or {}).get("model"), "haiku")

        # 4. Native Rust `terser-fast exec --sid` identical-outcome cmd_delta
        fast_bin = Path(__file__).resolve().parent.parent / "bin" / ("terser-fast.exe" if os.name == "nt" else "terser-fast")
        if fast_bin.is_file():
            py_cmd = "for i in range(25): print(f'module_{chr(65 + (i % 26))}_{chr(97 + ((i * 7) % 26))} stable output payload line')"
            r1 = subprocess.run(
                [str(fast_bin), "exec", "--host", "antigravity", "--sid", "nat-delta-1", "--", sys.executable, "-c", py_cmd],
                capture_output=True, text=True, env=self.env, timeout=10,
            )
            r2 = subprocess.run(
                [str(fast_bin), "exec", "--host", "antigravity", "--sid", "nat-delta-1", "--", sys.executable, "-c", py_cmd],
                capture_output=True, text=True, env=self.env, timeout=10,
            )
            self.assertEqual(r1.returncode, 0)
            self.assertEqual(r2.returncode, 0)
            self.assertIn("[Terser Delta] Identical output", r2.stdout)
            self.assertLess(len(r2.stdout), len(r1.stdout) * 0.5)

    def test_partial_overlap_trimming_sqlite_blast_radius_and_web_table_compaction(self) -> None:
        from runtime import index_mcp
        from runtime.config import DEFAULT_CONFIG

        cfg = dict(DEFAULT_CONFIG)

        # 1. Partial-overlap range trimming
        bigf = self.tmp / "overlap.py"
        bigf.write_text("\n".join(f"line_{i} = {i}" for i in range(1, 221)) + "\n", encoding="utf-8")
        self.assertIsNone(read_funnel.check_read("s-ov", str(bigf), start_line=1, end_line=100, cfg=cfg, env=self.env))
        trimmed = read_funnel.check_read("s-ov", str(bigf), start_line=1, end_line=180, cfg=cfg, env=self.env)
        self.assertIsNotNone(trimmed)
        self.assertEqual(trimmed["action"], "deny")
        self.assertIn("L101-L180", trimmed["reason"])
        # Immediate retry passes through!
        self.assertIsNone(read_funnel.check_read("s-ov", str(bigf), start_line=1, end_line=180, cfg=cfg, env=self.env))

        # 2. SQLite symbol + import-edge blast radius (filters incidental comment-only noise)
        ws = self.tmp / "graph_ws"
        ws.mkdir()
        (ws / "core.py").write_text("def compute_score(x):\n    return x * 2\n", encoding="utf-8")
        (ws / "caller.py").write_text("from core import compute_score\nres = compute_score(5)\n", encoding="utf-8")
        (ws / "unrelated.py").write_text("# Note: we discussed compute_score in docs\nVAL = 1\n", encoding="utf-8")

        res = index_mcp.index_folder(str(ws), env=self.env)
        repo = res["repo"]
        self.assertTrue((index_mcp.index_dir(self.env) / f"{repo}.db").is_file())
        br = index_mcp.dispatch_action("get_blast_radius", {"repo": repo, "symbol": "compute_score"}, env=self.env)
        files_hit = [item["file"] for item in br["impacted_files"]]
        self.assertIn("core.py", files_hit)
        self.assertIn("caller.py", files_hit)
        self.assertNotIn("unrelated.py", files_hit)

        # Incremental single-file reindex updates SQLite
        (ws / "extra.py").write_text("import core\nval = core.compute_score(9)\n", encoding="utf-8")
        self.assertTrue(index_mcp.reindex_file(str(ws / "extra.py"), env=self.env))
        br2 = index_mcp.dispatch_action("get_blast_radius", {"repo": repo, "symbol": "compute_score"}, env=self.env)
        self.assertIn("extra.py", [item["file"] for item in br2["impacted_files"]])

        # 3. HTML boilerplate stripping + Markdown padded table compaction
        html_doc = (
            "<!DOCTYPE html><html><head><style>body { color: red; " + "x" * 200 + " }</style>"
            "<script>console.log('tracking " + "y" * 200 + "');</script></head>"
            "<body><nav>Home | Docs | Pricing | Login | Careers</nav>"
            "<main><h1>API Reference</h1><p>Use <a href='https://example.com/auth'>OAuth2</a> tokens.</p></main>"
            "<footer>Copyright 2026 Example Corp All Rights Reserved</footer></body></html>"
        )
        comp_html = output_compressor.compact_web_and_markdown(html_doc)
        self.assertIn("API Reference", comp_html)
        self.assertIn("[OAuth2](https://example.com/auth)", comp_html)
        self.assertNotIn("console.log", comp_html)
        self.assertNotIn("Pricing", comp_html)
        self.assertLess(len(comp_html), len(html_doc) * 0.4)

        md_table = "\n".join([
            "| Endpoint             | Method   | Status     | Latency      |",
            "| -------------------- | -------- | ---------- | ------------ |",
            "| /api/v1/users        | GET      | 200 OK     | 12ms         |",
            "| /api/v1/orders       | POST     | 201 OK     | 34ms         |",
            "| /api/v1/inventory    | GET      | 200 OK     | 18ms         |",
            "| /api/v1/checkout     | POST     | 200 OK     | 45ms         |",
            "| /api/v1/webhooks     | PUT      | 204 OK     | 22ms         |",
        ])
        comp_md = output_compressor.compact_web_and_markdown(md_table)
        self.assertIn("/api/v1/users\tGET\t200 OK\t12ms", comp_md)
        self.assertNotIn("| ---", comp_md)
        self.assertLess(len(comp_md), len(md_table) * 0.65)

    def test_edit_seeding_large_slice_outline_and_lossless_compactors(self) -> None:
        from runtime import index_bridge, index_mcp
        from runtime.config import DEFAULT_CONFIG

        cfg = dict(DEFAULT_CONFIG)

        # 1. seed_after_edit: editing session gets T0 on re-read; sibling subagent is invalidated
        ef = self.tmp / "editable.py"
        ef.write_text("".join(f"v_{i} = {i}\n" for i in range(40)), encoding="utf-8")
        self.assertIsNone(read_funnel.check_read("parent-1", str(ef), cfg=cfg, env=self.env))
        self.assertIsNone(read_funnel.check_read("parent-1--sub1", str(ef), cfg=cfg, env=self.env))
        # Simulate successful edit_file by parent-1
        ef.write_text("".join(f"v_{i} = {i * 10}\n" if i == 5 else f"v_{i} = {i}\n" for i in range(40)), encoding="utf-8")
        read_funnel.seed_after_edit("parent-1", str(ef), old_text="v_5 = 5\n", new_text="v_5 = 50\n", env=self.env)
        # Parent re-read hits T0 unchanged (no self-diff echo!)
        parent_reread = read_funnel.check_read("parent-1", str(ef), cfg=cfg, env=self.env)
        self.assertIsNotNone(parent_reread)
        assert parent_reread is not None
        self.assertIn("unchanged", parent_reread["reason"])
        # Sibling subagent gets T1 diff (NOT stale T0!)
        sub_reread = read_funnel.check_read("parent-1--sub1", str(ef), cfg=cfg, env=self.env)
        self.assertIsNotNone(sub_reread)
        assert sub_reread is not None
        self.assertIn("+v_5 = 50", sub_reread["reason"])
        # Ranged-only session must NOT be seeded on partial edit (prevents shifted unread lines from hitting T0)
        rf = self.tmp / "ranged_edit.py"
        rf.write_text("".join(f"r_{i} = {i}\n" for i in range(60)), encoding="utf-8")
        self.assertIsNone(read_funnel.check_read("r-sess", str(rf), start_line=1, end_line=20, cfg=cfg, env=self.env))
        rf.write_text("".join(f"r_{i} = {i}\n" for i in range(10, 60)), encoding="utf-8")
        read_funnel.seed_after_edit("r-sess", str(rf), env=self.env)
        ranged_after = read_funnel.check_read("r-sess", str(rf), start_line=1, end_line=20, cfg=cfg, env=self.env)
        # Either passes through or returns diff — must NEVER say "unchanged"
        if ranged_after is not None:
            self.assertNotIn("unchanged", ranged_after["reason"])

        # 2. Large-slice Tier 2 outline guard on unread large file
        huge = self.tmp / "huge_mod.py"
        huge.write_text(
            "".join(
                f"def worker_{i}(arg):\n    '''Detailed worker docstring padding for realistic size {i}.'''\n    return arg + {i}\n\n"
                for i in range(140)
            ),
            encoding="utf-8",
        )
        self.assertGreater(huge.stat().st_size, 12288)
        res_slice = read_funnel.check_read("s-slice", str(huge), start_line=1, end_line=450, cfg=cfg, env=self.env)
        self.assertIsNotNone(res_slice)
        assert res_slice is not None
        self.assertIn("Outline", res_slice["reason"])
        self.assertIn("L1-L450", res_slice["reason"])
        # Immediate retry loads the requested slice
        self.assertIsNone(read_funnel.check_read("s-slice", str(huge), start_line=1, end_line=450, cfg=cfg, env=self.env))

        # 3. Lossless single-object JSON minification
        pretty_obj = json.dumps(
            {f"section_{i}": {"enabled": True, "timeout_ms": i * 100, "tags": ["alpha", "beta"]} for i in range(25)},
            indent=4,
        )
        crushed_obj = smart_crusher.crush_json(pretty_obj, env=self.env)
        self.assertIsNotNone(crushed_obj)
        assert crushed_obj is not None
        self.assertLess(len(crushed_obj), len(pretty_obj) * 0.80)
        self.assertEqual(json.loads(crushed_obj), json.loads(pretty_obj))

        # 4. Space-padded CLI table compaction
        cli_table = "\n".join([
            "CONTAINER ID        IMAGE                       STATUS              NAMES",
            "a1b2c3d4e5f6        postgres:16-alpine          Up 3 hours          app-db-primary",
            "b2c3d4e5f6a7        redis:7-alpine              Up 3 hours          app-cache-main",
            "c3d4e5f6a7b8        nginx:1.25-alpine           Up 2 hours          app-edge-proxy",
            "d4e5f6a7b8c9        myapp/api:latest            Up 1 hour           app-api-server",
            "e5f6a7b8c9d0        myapp/worker:latest         Up 1 hour           app-bg-worker",
        ])
        comp_tbl = output_compressor.compact_aligned_tables(cli_table)
        self.assertIn("a1b2c3d4e5f6\tpostgres:16-alpine\tUp 3 hours\tapp-db-primary", comp_tbl)
        self.assertLess(len(comp_tbl), len(cli_table) * 0.85)

        # 5. Reversible base64 / data-URI blob folding
        b64_payload = "A" * 1200
        raw_b64 = f'{{"status":"ok","preview":"data:image/png;base64,{b64_payload}"}}'
        folded_b64, b64_aid = output_compressor.fold_base64_blobs(raw_b64, command="mcp_screenshot", env=self.env)
        self.assertIsNotNone(b64_aid)
        self.assertIn("[data:image/png;base64", folded_b64)
        self.assertLess(len(folded_b64), 200)
        restored = output_compressor.read_archived_output(b64_aid or "", env=self.env)
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertIn(b64_payload, restored)

        # 6. Directory-prefix factoring for file trees & Glob
        paths = [
            f"src/features/dashboard/components/Widget{i}.tsx" for i in range(8)
        ] + [
            f"src/features/auth/hooks/useAuth{i}.ts" for i in range(4)
        ]
        comp_tree = output_compressor.compact_file_list(paths)
        self.assertIn("src/features/dashboard/components/: Widget0.tsx", comp_tree)
        self.assertIn("src/features/auth/hooks/: useAuth0.ts", comp_tree)
        self.assertLess(len(comp_tree), len("\n".join(paths)) * 0.65)

        # 7. SQLite-first index (.meta.json + .db) + compact get_file_tree MCP response
        ws = self.tmp / "sqlite_ws"
        (ws / "pkg" / "sub").mkdir(parents=True)
        for i in range(6):
            (ws / "pkg" / "sub" / f"mod_{i}.py").write_text(f"def fn_{i}():\n    return {i}\n", encoding="utf-8")
        idx_res = index_mcp.index_folder(str(ws), env=self.env)
        repo = idx_res["repo"]
        self.assertTrue((index_mcp.index_dir(self.env) / f"{repo}.meta.json").is_file())
        self.assertTrue((index_mcp.index_dir(self.env) / f"{repo}.db").is_file())
        self.assertFalse((index_mcp.index_dir(self.env) / f"{repo}.json").exists())
        summary = index_bridge.workspace_index_summary(str(ws), env=self.env)
        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary["repo"], repo)
        tree_payload = index_mcp.dispatch_action("get_file_tree", {"repo": repo}, env=self.env)
        tree_text = index_mcp.format_mcp_response("get_file_tree", tree_payload)
        self.assertIn("pkg/sub/: mod_0.py", tree_text)

        # 8. Native Rust `terser-fast exec` strips lossless noise even on non-zero exit
        fast_bin = ROOT / "bin" / ("terser-fast.exe" if os.name == "nt" else "terser-fast")
        if fast_bin.is_file():
            fail_script = (
                "import sys\n"
                "for i in range(25): print(f'\\x1b[32m   Compiling crate_{i} v0.1.0\\x1b[0m')\n"
                "print('\\x1b[31merror[E0308]: mismatched types\\x1b[0m')\n"
                "sys.exit(101)\n"
            )
            r = subprocess.run(
                [str(fast_bin), "exec", "--host", "antigravity", "--", sys.executable, "-c", fail_script],
                capture_output=True, text=True, env=self.env, timeout=10,
            )
            self.assertEqual(r.returncode, 101)
            self.assertIn("error[E0308]: mismatched types", r.stdout)
            self.assertNotIn("\x1b[", r.stdout)


class LosslessParityAndCoordinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = {**os.environ, "TERSER_HOME": str(self.root / "home")}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dedup_identical_tracebacks(self) -> None:
        tb = (
            "Traceback (most recent call last):\n"
            '  File "app.py", line 10, in handle\n'
            "    return parse(x)\n"
            '  File "parser.py", line 42, in parse\n'
            "    raise ValueError('bad token')\n"
            "ValueError: bad token\n"
        )
        raw = f"FAILED test_a[1]\n{tb}FAILED test_a[2]\n{tb}FAILED test_a[3]\n{tb}"
        out = output_compressor.dedup_identical_tracebacks(raw)
        self.assertEqual(out.count("Traceback (most recent call last):"), 1)
        self.assertEqual(out.count("identical traceback to #1 omitted"), 2)
        self.assertIn("ValueError: bad token", out)

    def test_mixed_ndjson_crusher_preserves_banners(self) -> None:
        from runtime import smart_crusher

        banner = "INFO: Server listening on 0.0.0.0:8080\n"
        ndjson_lines = [
            json.dumps({
                "service": "auth-api",
                "env": "prod",
                "level": "INFO",
                "request_id": f"req-{i:04d}",
                "latency_ms": 10 + i,
                "message": f"handled login request for user_{i} cleanly",
            })
            for i in range(15)
        ]
        raw = banner + "\n".join(ndjson_lines) + "\nINFO: Shutdown complete"
        crushed = smart_crusher.crush_mixed_ndjson(raw, env=self.env)
        self.assertIsNotNone(crushed)
        assert crushed is not None
        self.assertIn("Server listening on 0.0.0.0:8080", crushed)
        self.assertIn("Shutdown complete", crushed)
        self.assertIn('"_format":"columnar"', crushed)
        self.assertLess(len(crushed), int(len(raw) * 0.80))

    def test_sandbox_content_addressed_dedup_and_chunk_overlap(self) -> None:
        from runtime import context_sandbox

        lines = [f"line {i:03d}: routine status output" for i in range(1, 65)]
        lines[29] = "line 030: TriggerEventAlpha"
        lines[30] = "line 031: RootCauseBeta details"
        text = "\n".join(lines)

        aid1 = context_sandbox.store_output(text, command="cmd1", env=self.env)
        aid2 = context_sandbox.store_output(text, command="cmd2", env=self.env)
        self.assertIsNotNone(aid1)
        self.assertEqual(aid1, aid2)

        # Boundary overlap ensures searching for line 30 also captures line 31 in the same/adjacent chunk
        res = context_sandbox.query_output(aid1 or "", "RootCauseBeta", env=self.env)
        self.assertIsNotNone(res)
        assert res is not None
        self.assertIn("TriggerEventAlpha", res)
        self.assertIn("RootCauseBeta", res)

    def test_session_brief_static_first_ordering(self) -> None:
        from runtime import context_sandbox, index_mcp, session_brief

        ws = self.root / "proj"
        ws.mkdir()
        (ws / "main.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
        index_mcp.index_folder(str(ws), env=self.env)
        context_sandbox.record_session_event("s1", str(ws), "edit", "main.py", env=self.env)

        brief, _ = session_brief.build_session_brief(
            str(ws), host="antigravity", cfg=config.load_config(self.env), env=self.env
        )
        self.assertIsNotNone(brief)
        assert brief is not None
        self.assertIn("Code index:", brief)
        self.assertIn("Prior session:", brief)
        self.assertLess(brief.index("Code index:"), brief.index("Prior session:"))

    def test_claude_post_tool_use_compresses_stderr(self) -> None:
        cfg = config.load_config(self.env)
        noisy_stderr = "\n".join(
            [f"   Compiling crate_{i} v0.1.0" for i in range(30)]
            + ["error[E0308]: mismatched types"]
        )
        payload = {
            "session_id": "s-stderr",
            "cwd": str(self.root),
            "tool_name": "Bash",
            "tool_input": {"command": "cargo build"},
            "tool_response": {"stdout": "", "stderr": noisy_stderr},
            "error": "exit 101",
        }
        res = unified_hook.handle_post_tool_use("claude", payload, cfg, self.env, is_failure_event=True)
        updated = (res.get("hookSpecificOutput") or {}).get("updatedToolOutput") or {}
        self.assertIn("stderr", updated)
        self.assertIn("error[E0308]: mismatched types", updated["stderr"])
        self.assertLess(len(updated["stderr"]), len(noisy_stderr))

    def test_outline_cache_and_index_read_seeding_and_partial_edit_seed(self) -> None:
        cfg = config.load_config(self.env)
        big = self.root / "big_mod.py"
        body = "\n".join(
            f'def func_{i}(x):\n    """Compute transformed value for index {i}."""\n    val = x + {i}\n    return val * 2\n'
            for i in range(180)
        )
        big.write_text(body, encoding="utf-8")

        # 1. Tier 2 writes outline-cache/{sha[:24]}.txt
        r1 = read_funnel.check_read("sess-a", str(big), cfg=cfg, host="antigravity", env=self.env)
        self.assertIsNotNone(r1)
        cache_dir = Path(self.env["TERSER_HOME"]) / "outline-cache"
        self.assertTrue(any(cache_dir.glob("*.txt")))

        # 2. Seeding interval from terser-index output causes overlapping read_file to hit T0
        read_funnel.seed_read_interval("sess-b", str(big), 10, 40, env=self.env)
        r_hit = read_funnel.check_read(
            "sess-b", str(big), start_line=15, end_line=35, cfg=cfg, host="antigravity", env=self.env
        )
        self.assertIsNotNone(r_hit)
        assert r_hit is not None
        self.assertEqual(r_hit["action"], "deny")
        self.assertIn("unchanged", r_hit["reason"])

        # 3. Line-preserving edit inside covered interval keeps cache seeded (T0 hit on re-read)
        old_line = "def func_5(x):"
        new_line = "def func_5(y):"
        big.write_text(body.replace(old_line, new_line, 1), encoding="utf-8")
        read_funnel.seed_after_edit(
            "sess-b", str(big), old_text=old_line, new_text=new_line, env=self.env
        )
        r_after = read_funnel.check_read(
            "sess-b", str(big), start_line=15, end_line=35, cfg=cfg, host="antigravity", env=self.env
        )
        self.assertIsNotNone(r_after)
        assert r_after is not None
        self.assertIn("unchanged", r_after["reason"])

    def test_sqlite_first_index_actions_without_json(self) -> None:
        from runtime import index_mcp

        ws = self.root / "sqlrepo"
        ws.mkdir()
        (ws / "alpha.py").write_text("def target_fn(x):\n    return x + 1\n", encoding="utf-8")
        (ws / "beta.py").write_text("from alpha import target_fn\n\ndef caller():\n    return target_fn(2)\n", encoding="utf-8")

        res = index_mcp.index_folder(str(ws), env=self.env)
        slug = res["repo"]
        # Remove the monolithic JSON file to prove all actions run purely from SQLite + .meta.json
        json_file = index_mcp.index_dir(self.env) / f"{slug}.json"
        if json_file.is_file():
            json_file.unlink()

        outline = index_mcp.dispatch_action("get_repo_outline", {"repo": slug}, env=self.env)
        self.assertIn("outline", outline)
        self.assertTrue(any("target_fn" in ln for ln in outline["outline"]))

        batch = index_mcp.dispatch_action(
            "get_symbols", {"repo": slug, "symbol_ids": ["alpha.py::target_fn"]}, env=self.env
        )
        self.assertEqual(len(batch.get("symbols") or []), 1)
        self.assertIn("return x + 1", batch["symbols"][0]["source"])

        ctx = index_mcp.dispatch_action(
            "assemble_task_context", {"repo": slug, "task": "target_fn", "token_budget": 2000}, env=self.env
        )
        self.assertTrue(ctx.get("symbols"))

        blast = index_mcp.dispatch_action(
            "get_blast_radius", {"repo": slug, "symbol": "target_fn"}, env=self.env
        )
        files = [item["file"] for item in blast.get("impacted_files") or []]
        self.assertIn("alpha.py", files)
        self.assertIn("beta.py", files)

    def test_zero_quality_loss_upgrades_suite(self) -> None:
        from runtime import cmd_delta, index_mcp, smart_crusher

        cfg = config.load_config(self.env)

        # 1. Cross-tool terser-index -> read_funnel seeding regex matches single-L `L10-50` format
        ws = self.root / "seed_ws"
        ws.mkdir()
        mod = ws / "app.py"
        mod.write_text("\n".join(f"line_{i} = {i}" for i in range(1, 80)), encoding="utf-8")
        mcp_out = "app.py::fn (app.py L10-40):\nline_10 = 10\n"
        unified_hook._seed_reads_from_index_output("sess-seed", str(ws), mcp_out, self.env)
        r_seeded = read_funnel.check_read(
            "sess-seed", str(mod), start_line=12, end_line=35, cfg=cfg, host="antigravity", env=self.env
        )
        self.assertIsNotNone(r_seeded)
        assert r_seeded is not None
        self.assertIn("unchanged", r_seeded["reason"])

        # 2. Non-Python T2 outline includes exact Lstart-Lend spans
        rs_file = ws / "lib.rs"
        rs_body = "\n".join(
            f"pub fn handler_{i}(x: i32) -> i32 {{\n    let y = x + {i};\n    y * 2\n}}\n"
            for i in range(4)
        )
        rs_file.write_text(rs_body, encoding="utf-8")
        outline = read_funnel.build_code_outline(str(rs_file), rs_body, env=self.env)
        self.assertIsNotNone(outline)
        assert outline is not None
        self.assertRegex(outline, r"L1-4:.*pub fn handler_0")

        # 3. Edit invalidates terser-index MCP dedup entries so fresh symbol lookup isn't soft-denied
        first_mcp = read_funnel.check_mcp_refetch(
            "sess-mcp", "terser_terser-index:get_symbol_source", {"repo": "r", "symbol_id": "a"},
            cfg=cfg, host="antigravity", env=self.env,
        )
        self.assertIsNone(first_mcp)
        read_funnel.record_mcp_response_size(
            "sess-mcp", "terser_terser-index:get_symbol_source", {"repo": "r", "symbol_id": "a"},
            4321, env=self.env,
        )
        read_funnel.invalidate_on_edit("sess-mcp", str(mod), env=self.env)
        after_edit_mcp = read_funnel.check_mcp_refetch(
            "sess-mcp", "terser_terser-index:get_symbol_source", {"repo": "r", "symbol_id": "a"},
            cfg=cfg, host="antigravity", env=self.env,
        )
        self.assertIsNone(after_edit_mcp)

        # 4. Lossless git diff header compaction preserves hunks & lines verbatim
        raw_diff = (
            "diff --git a/src/foo.py b/src/foo.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/src/foo.py\n"
            "+++ b/src/foo.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-old = 1\n"
            "+new = 2\n"
        )
        comp_diff = output_compressor.compact_diff_headers(raw_diff)
        self.assertIn("=== src/foo.py", comp_diff)
        self.assertNotIn("index 1111111", comp_diff)
        self.assertIn("@@ -1,3 +1,3 @@", comp_diff)
        self.assertIn("+new = 2", comp_diff)

        # 5. SmartCrusher wide API payload (>30 raw keys, mostly const/null) + multi-array + lossless medium (50 items)
        wide_items = [
            {
                **{f"const_{k}": "same" for k in range(25)},
                **{f"null_{k}": None for k in range(10)},
                "id": i,
                "name": f"item_{i}",
            }
            for i in range(50)
        ]
        wide_payload = json.dumps({"added": wide_items, "removed": wide_items[:8]})
        crushed_wide = smart_crusher.crush_json(wide_payload, env=self.env)
        self.assertIsNotNone(crushed_wide)
        assert crushed_wide is not None
        parsed = json.loads(crushed_wide)
        self.assertEqual(parsed["added"]["_format"], "columnar")
        self.assertEqual(len(parsed["added"]["rows"]), 50)  # Lossless for 50 short items
        self.assertEqual(parsed["removed"]["_format"], "columnar")

        # 6. Broadened cmd_delta eligibility & WebFetch PostToolUse compression
        self.assertTrue(cmd_delta._DELTA_ELIGIBLE_RE.match("npm run lint"))
        self.assertTrue(cmd_delta._DELTA_ELIGIBLE_RE.match("gh pr checks 42"))
        self.assertTrue(cmd_delta._DELTA_ELIGIBLE_RE.match("dotnet test"))
        raw_web = (
            "<html><head><script>var analytics = 'x' * 500; console.log('tracking');</script>"
            "<style>.nav { display: flex; padding: 12px; color: #fff; }</style></head>"
            "<body><nav>Home | Docs | Pricing | Blog | Login | Sign Up</nav>"
            "<main><h1>API Reference</h1><p>Useful documentation line.</p></main>"
            "<footer>Copyright 2026 Example Corp. Privacy Policy. Terms of Service.</footer></body></html>\n"
            + ("| Column One          | Column Two          | Column Three        |\n"
               "| ------------------- | ------------------- | ------------------- |\n"
               + "| alpha_value         | beta_value          | gamma_value         |\n" * 15)
        )
        web_res = unified_hook.handle_post_tool_use(
            "claude",
            {"session_id": "s-web", "tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}, "tool_response": raw_web},
            cfg, self.env,
        )
        web_out = (web_res.get("hookSpecificOutput") or {}).get("updatedToolOutput") or ""
        self.assertTrue(web_out)
        self.assertIn("Useful documentation line.", web_out)
        self.assertNotIn("console.log", web_out)
        self.assertLess(len(web_out), len(raw_web))

        # 7. Single-pass Python AST extractor returns both symbols and imports
        py_src = "import os\nfrom pathlib import Path\n\ndef work():\n    import json\n    return json.dumps({})\n"
        syms, imps = index_mcp._extract_python_symbols_and_imports("work.py", py_src)
        self.assertEqual([s["name"] for s in syms], ["work"])
        self.assertTrue({"os", "pathlib", "Path", "json"}.issubset(imps))

    def test_zero_loss_round4_upgrades(self) -> None:
        from runtime import index_mcp

        cfg = config.load_config(self.env)

        # 1. Medium MCP response (1.5 KB-16 KB) gets strictly lossless compression (zero items dropped)
        med_items = [
            {"id": i, "state": "open", "repo": "terser", "title": f"Issue number {i}"}
            for i in range(60)
        ]
        raw_mcp = json.dumps({"items": med_items}, indent=2)
        self.assertGreater(len(raw_mcp), 1500)
        self.assertLess(len(raw_mcp), 16384)
        mcp_res = unified_hook.handle_post_tool_use(
            "claude",
            {
                "session_id": "s-med-mcp",
                "tool_name": "mcp__github__list_issues",
                "tool_input": {"repo": "terser"},
                "tool_response": raw_mcp,
            },
            cfg,
            self.env,
        )
        comp_mcp = (mcp_res.get("hookSpecificOutput") or {}).get("updatedMCPToolOutput") or ""
        self.assertTrue(comp_mcp)
        self.assertLess(len(comp_mcp), len(raw_mcp) * 0.70)
        parsed_mcp = json.loads(comp_mcp)
        self.assertEqual(parsed_mcp["items"]["_format"], "columnar")
        self.assertEqual(len(parsed_mcp["items"]["rows"]), 60)  # 100% lossless, zero sampling

        # 2. Mixed NDJSON inside server logs is wired into output_compressor.compress_output()
        mixed_log = (
            "=== Server startup complete ===\n"
            + "\n".join(
                json.dumps({"level": "info", "service": "api-gw", "region": "us-east-1", "req_id": i, "ms": 10 + i})
                for i in range(25)
            )
            + "\n=== Shutdown complete ===\n"
        )
        comp_mixed, _ = output_compressor.compress_output(mixed_log, command="node server.js", env=self.env)
        self.assertIn("=== Server startup complete ===", comp_mixed)
        self.assertIn('"_format":"columnar"', comp_mixed)
        self.assertLess(len(comp_mixed), len(mixed_log) * 0.75)

        # 3. Plain-text format_mcp_response for remaining terser-index actions
        self.assertEqual(
            index_mcp.format_mcp_response("search_sandbox", {"archive_id": "abc123", "result": "--- L1-L5 ---\nhello"}),
            "--- L1-L5 ---\nhello",
        )
        exp_txt = index_mcp.format_mcp_response("expand_sandbox", {"archive_id": "abc123", "content": "L1: alpha\nL2: beta"})
        self.assertNotIn("\\n", exp_txt)
        self.assertIn("L1: alpha", exp_txt)
        out_txt = index_mcp.format_mcp_response(
            "get_repo_outline",
            {"repo": "demo", "files": 2, "symbols": 4, "outline": ["a.py: foo, bar"]},
        )
        self.assertIn("a.py: foo, bar", out_txt)
        self.assertFalse(out_txt.startswith("{"))
        repos_txt = index_mcp.format_mcp_response(
            "list_repos",
            {"repos": [{"repo": "demo", "files": 2, "symbols": 4, "source_root": "/tmp/demo"}]},
        )
        self.assertIn("demo (2 files, 4 symbols)", repos_txt)
        menu_txt = index_mcp.format_mcp_response("menu", {"actions": {"list_repos": {"args": "{}", "desc": "List repos"}}})
        self.assertIn("list_repos {}", menu_txt)
        exec_txt = index_mcp.format_mcp_response(
            "execute_filtered",
            {"returncode": 0, "filtered": True, "raw_bytes": 9000, "archive_id": "deadbeef1234", "output": "line1\nline2"},
        )
        self.assertIn("line1\nline2", exec_txt)
        self.assertNotIn("\\n", exec_txt)

        # 4. Claude/Codex PostToolUse Bash stdout threshold lowered to >300 chars
        med_table = "\n".join(
            [f"CONTAINER_{i}        nginx:latest          Up 2 hours          0.0.0.0:808{i}->80/tcp" for i in range(8)]
        )
        self.assertGreater(len(med_table), 300)
        self.assertLess(len(med_table), 1500)
        bash_res = unified_hook.handle_post_tool_use(
            "claude",
            {
                "session_id": "s-med-bash",
                "tool_name": "Bash",
                "tool_input": {"command": "docker ps"},
                "tool_response": {"stdout": med_table, "stderr": ""},
            },
            cfg,
            self.env,
        )
        upd_stdout = ((bash_res.get("hookSpecificOutput") or {}).get("updatedToolOutput") or {}).get("stdout") or ""
        self.assertTrue(upd_stdout)
        self.assertIn("\t", upd_stdout)
        self.assertLess(len(upd_stdout), len(med_table) * 0.85)

        # 5. compact_file_list applied to shell file-listing commands (git ls-files)
        ls_raw = "\n".join(f"src/components/widgets/widget_{i}.tsx" for i in range(12))
        ls_comp, _ = output_compressor.compress_output(ls_raw, command="git ls-files", env=self.env)
        self.assertIn("src/components/widgets/: ", ls_comp)
        self.assertLess(len(ls_comp), len(ls_raw) * 0.70)

        # 6. seed_after_edit shifts cached intervals on line-adding edits (exact line alignment)
        work_dir = Path(self.env["TERSER_HOME"]) / "round4_work"
        work_dir.mkdir(parents=True, exist_ok=True)
        shift_file = work_dir / "shift_demo.py"
        orig_lines = [f"line_{i} = {i}" for i in range(1, 101)]
        shift_file.write_text("\n".join(orig_lines) + "\n", encoding="utf-8")
        self.assertIsNone(read_funnel.check_read("s-shift", str(shift_file), start_line=1, end_line=30, cfg=cfg, env=self.env))
        self.assertIsNone(read_funnel.check_read("s-shift", str(shift_file), start_line=60, end_line=80, cfg=cfg, env=self.env))
        # Insert 3 extra lines at line 10 (inside [1, 30]) -> [1, 30] becomes [1, 33], [60, 80] becomes [63, 83]
        old_s = "line_10 = 10"
        new_s = "line_10 = 10\nextra_1 = 1\nextra_2 = 2\nextra_3 = 3"
        shift_file.write_text(shift_file.read_text(encoding="utf-8").replace(old_s, new_s, 1), encoding="utf-8")
        read_funnel.seed_after_edit("s-shift", str(shift_file), old_text=old_s, new_text=new_s, env=self.env)
        # Shifted second interval [63, 83] must hit Tier 0 unchanged!
        hit = read_funnel.check_read("s-shift", str(shift_file), start_line=63, end_line=83, cfg=cfg, env=self.env)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["action"], "deny")
        self.assertIn("unchanged", hit["reason"])
        # Unread gap [35, 55] must still be allowed!
        self.assertIsNone(read_funnel.check_read("s-shift", str(shift_file), start_line=35, end_line=55, cfg=cfg, env=self.env))

        # 7. Tier 1 delta-diff updates mtime_ns and size so next unchanged re-read hits stat fastpath
        t1_file = work_dir / "tier1_stat.py"
        t1_file.write_text("\n".join(f"x_{i} = {i}" for i in range(60)) + "\n", encoding="utf-8")
        self.assertIsNone(read_funnel.check_read("s-t1stat", str(t1_file), cfg=cfg, env=self.env))
        t1_file.write_text(t1_file.read_text(encoding="utf-8").replace("x_5 = 5", "x_5 = 555", 1), encoding="utf-8")
        t1_res = read_funnel.check_read("s-t1stat", str(t1_file), cfg=cfg, env=self.env)
        self.assertIsNotNone(t1_res)
        self.assertIn("Exact unified diff", t1_res["reason"])
        store = read_funnel._load_store("s-t1stat", self.env)
        entry = store["reads"][read_funnel._norm_path(str(t1_file))]
        st_now = t1_file.stat()
        self.assertEqual(entry.get("mtime_ns"), st_now.st_mtime_ns)
        self.assertEqual(entry.get("size"), st_now.st_size)

        # 8. Optional repo auto-resolution in index_mcp (sole indexed repo)
        idx_repo = work_dir / "auto_repo"
        idx_repo.mkdir()
        (idx_repo / "calc.py").write_text("def multiply_values(a, b):\n    return a * b\n", encoding="utf-8")
        idx_env = dict(self.env, TERSER_INDEX_PATH=str(work_dir / "auto-idx"))
        index_mcp.index_folder(str(idx_repo), env=idx_env)
        auto_res = index_mcp.dispatch_action("search_symbols", {"query": "multiply_values"}, env=idx_env)
        self.assertEqual(len(auto_res.get("matches") or []), 1)
        self.assertEqual(auto_res["matches"][0]["name"], "multiply_values")

        # 9. context_sandbox transparently lazy-indexes Rust raw-archives/<id>.txt
        raw_dir = Path(self.env["TERSER_HOME"]) / "raw-archives"
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_id = "abcdef123456"
        (raw_dir / f"{raw_id}.txt").write_text(
            "build start\nerror[E0425]: cannot find value `missing_token`\nbuild end\n",
            encoding="utf-8",
        )
        self.assertEqual(context_sandbox.get_latest_archive_id(self.env), raw_id)
        q_res = context_sandbox.query_output(raw_id, "missing_token", env=self.env)
        self.assertIsNotNone(q_res)
        self.assertIn("missing_token", q_res or "")
        e_res = context_sandbox.expand_output(raw_id, start_line=2, end_line=2, env=self.env)
        self.assertIn("L2: error[E0425]", e_res or "")


_FAST_BIN = ROOT / "bin" / ("terser-fast.exe" if os.name == "nt" else "terser-fast")


@unittest.skipUnless(_FAST_BIN.is_file(), "terser-fast binary not built")
class NativeGateAndWorkerTests(unittest.TestCase):
    """Quality-neutral fast paths: Rust may only skip Python when Python would do nothing."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.state = self.tmp / "state"
        self.marker = self.tmp / "delegated.txt"
        self.probe = self.tmp / "probe_hook.py"
        self.probe.write_text(
            "open(%r, 'a').write('x')\n" % str(self.marker),
            encoding="utf-8",
        )
        self.env = dict(
            os.environ,
            TERSER_HOME=str(self.state),
            TOKENECONOMIZER_HOME=str(self.tmp / "te"),
            TERSER_PYTHON_WORKER="0",
        )
        self.env.pop("TERSER_DISABLE", None)

    def tearDown(self) -> None:
        for info in self.state.glob("hook-worker-*.json"):
            try:
                os.kill(int(json.loads(info.read_text(encoding="utf-8"))["pid"]), 9)
            except Exception:
                pass
        self._td.cleanup()

    def _run(self, event: str, payload: dict, *, script: Path, env: dict | None = None) -> str:
        proc = subprocess.run(
            [str(_FAST_BIN), "--python", sys.executable, "--script", str(script), "--host", "claude", "--event", event],
            input=json.dumps(payload), capture_output=True, text=True, env=env or self.env, timeout=30,
        )
        return proc.stdout

    def _delegated(self) -> bool:
        hit = self.marker.exists()
        if hit:
            self.marker.unlink()
        return hit

    def test_bash_gate_skips_python_only_when_python_recorded_no_repairs(self) -> None:
        payload = {"session_id": "gate-1", "cwd": str(ROOT), "tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
        self._run("PreToolUse", payload, script=self.probe)
        self.assertTrue(self._delegated(), "no gate entry yet -> must delegate")

        cfg = config.load_config(self.env)
        out, repaired = loop_guard.apply_form_repair(str(ROOT), "pytest -q", cfg=cfg, host="claude", env=self.env)
        self.assertEqual((out, repaired), ("pytest -q", False))
        gate = json.loads((self.state / loop_guard.FORM_REPAIR_GATE_FILE).read_text(encoding="utf-8"))
        self.assertIs(gate[f"claude|{ROOT}"]["has"], False)

        self._run("PreToolUse", payload, script=self.probe)
        self.assertFalse(self._delegated(), "recorded no-repair checkout -> Rust answers alone")

        # A changed environment input invalidates the entry.
        self._run("PreToolUse", payload, script=self.probe, env=dict(self.env, TOKENECONOMIZER_REPO="x/y"))
        self.assertTrue(self._delegated())

    def test_grep_prompt_and_stop_gates(self) -> None:
        self._run("PreToolUse", {"session_id": "g", "tool_name": "Grep", "tool_input": {"pattern": "def main"}}, script=self.probe)
        self.assertFalse(self._delegated(), "no code index on disk -> nothing to answer")
        (self.state / "code-index").mkdir(parents=True)
        (self.state / "code-index" / "demo.meta.json").write_text("{}", encoding="utf-8")
        self._run("PreToolUse", {"session_id": "g", "tool_name": "Grep", "tool_input": {"pattern": "def main"}}, script=self.probe)
        self.assertTrue(self._delegated())

        self._run("UserPromptSubmit", {"session_id": "p1", "prompt": "hi"}, script=self.probe)
        self.assertFalse(self._delegated(), "no queued nudge -> nothing to pop")
        (self.state / "loop-state").mkdir(parents=True, exist_ok=True)
        (self.state / "loop-state" / "p1.json").write_text(json.dumps({"pending_nudge": "slow down"}), encoding="utf-8")
        self._run("UserPromptSubmit", {"session_id": "p1", "prompt": "hi"}, script=self.probe)
        self.assertTrue(self._delegated())

        (self.state / "rollup-claude.lease").touch()
        self._run("Stop", {"session_id": "p1"}, script=self.probe)
        self.assertFalse(self._delegated(), "lease held -> fold would be skipped")

    def test_worker_output_matches_cold_run(self) -> None:
        script = ROOT / "runtime" / "unified_hook.py"
        payload = {"session_id": "w1", "cwd": str(self.tmp), "tool_name": "Edit",
                   "tool_input": {"file_path": str(self.tmp / "a.py")}, "tool_response": {}}
        warm_env = dict(self.env, TERSER_PYTHON_WORKER="1")
        cold = [self._run("PostToolUse", dict(payload, session_id="cold"), script=script) for _ in range(4)]
        self._run("PostToolUse", dict(payload, session_id="spawn"), script=script, env=warm_env)
        import time as _time
        deadline = _time.time() + 15
        while _time.time() < deadline and not list(self.state.glob("hook-worker-*.json")):
            _time.sleep(0.1)
        self.assertTrue(list(self.state.glob("hook-worker-*.json")), "worker should start")
        warm = [self._run("PostToolUse", dict(payload, session_id="warm"), script=script, env=warm_env) for _ in range(4)]
        self.assertEqual(warm, cold)
        self.assertTrue(any("ran 3x identically" in o for o in warm), "stateful loop guard still fires via worker")


if __name__ == "__main__":
    unittest.main()
