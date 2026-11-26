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
            ".mcp.json",
            ".claude-plugin/plugin.json",
            ".claude-plugin/marketplace.json",
            ".codex-plugin/plugin.json",
            "hooks/hooks.json",
            "hooks/codex-hooks.json",
        ):
            data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
            self.assertIsInstance(data, dict, f"{rel} should be a JSON object")

        # Verify Counter front door enforced across all MCP configs
        for rel in ("mcp_config.json", ".mcp.json", ".claude-plugin/plugin.json"):
            doc = json.loads((ROOT / rel).read_text(encoding="utf-8"))
            env_map = doc["mcpServers"]["terser-index"]["env"]
            self.assertEqual(env_map["TERSER_TOOL_SURFACE"], "counter")

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
        self.assertIn("terse query", capped)
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
        self.assertIn("terse query", inner["_retrieve"])

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


if __name__ == "__main__":
    unittest.main()
