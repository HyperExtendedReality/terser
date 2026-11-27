from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime import codex_doctor, codex_hooks, config, index_mcp, session_brief, unified_hook
from scripts import install

# Hook subprocesses in tests must not leave warm workers behind (each test has its own TERSER_HOME).
os.environ.setdefault("TERSER_PYTHON_WORKER", "0")

ROOT = Path(__file__).resolve().parents[1]


class CodexPluginTests(unittest.TestCase):
    def test_malformed_mcp_request_does_not_close_stdio(self):
        requests = [
            {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            {"id": 2, "method": "tools/list"},
            {"id": 3, "method": "tools/call", "params": {"name": "order", "arguments": {
                "action": "search_sandbox", "args": {"limit": "invalid"},
            }}},
            {"id": 4, "method": "ping"},
        ]
        stdin = "[]\n" + "".join(json.dumps(dict(req, jsonrpc="2.0")) + "\n" for req in requests)
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "runtime" / "index_mcp.py")],
            input=stdin, capture_output=True, text=True, timeout=8, check=True,
        )
        invalid, init, tools, rejected, ping = map(json.loads, result.stdout.splitlines())
        self.assertEqual(invalid["error"]["code"], -32600)
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(len(tools["result"]["tools"]), 6)
        self.assertEqual(rejected["error"]["code"], -32602)
        self.assertEqual(ping["result"], {})

    def test_stale_index_reads_exact_current_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            env = {"TERSER_HOME": tmp}
            path = root / "example.py"
            path.write_text("def target():\n    return 1\n", encoding="utf-8")
            repo = index_mcp.index_folder(str(root), env=env)["repo"]
            args = {"repo": repo, "symbol_id": "example.py::target"}
            path.write_text("# prepended\n\ndef target():\n    return 2\n", encoding="utf-8")
            out = index_mcp.dispatch_action("get_symbol_source", dict(args, start_line=4, end_line=4), env=env)
            self.assertEqual(out["source"], "4:     return 2")
            outline = index_mcp.dispatch_action("get_file_outline", {"repo": repo, "file_path": "example.py"}, env=env)
            self.assertEqual(outline["symbols"][0]["start_line"], 3)
            self.assertIn("error", index_mcp.dispatch_action("get_symbol_source", dict(args, end_line=2), env=env))

    def test_codex_wire_format_preserves_tool_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"TERSER_HOME": tmp}
            cfg = dict(config.DEFAULT_CONFIG, codex_form_repair=True)
            raw = {"stdout": "output\n" * 120, "stderr": "failure\n", "exit_code": 2}
            shell = {"cwd": tmp, "tool_name": "Bash", "tool_input": {"command": "tool", "description": "label"}}
            with patch.object(unified_hook.loop_guard, "apply_form_repair", return_value=("tool.exe", True)):
                self.assertIsNone(unified_hook.handle_pre_tool_use("codex", shell, config.DEFAULT_CONFIG, env))
                allowed = unified_hook.handle_pre_tool_use("codex", shell, cfg, env)["hookSpecificOutput"]
            self.assertEqual(allowed["permissionDecision"], "allow")
            self.assertEqual(allowed["updatedInput"], {"command": "tool.exe", "description": "label"})
            with patch.object(unified_hook.output_compressor, "compress_output", side_effect=AssertionError):
                post = unified_hook.handle_post_tool_use("codex", dict(shell, tool_response=raw), cfg, env)
            self.assertEqual(post, {})
            self.assertEqual(raw["exit_code"], 2)

    def test_codex_rewrite_keeps_executable_and_arguments(self):
        python = Path(sys.executable)
        original = python.name
        replacement = str(python.parent).replace("\\", "/") + "/./" + original
        if any(c.isspace() for c in replacement):
            self.skipTest("Executable is outside the literal-path rewrite subset")
        args = ["-c", "import sys; print('stdout'); print('stderr', file=sys.stderr); sys.exit(7)"]
        suffix = f' -c "{args[1]}"'
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PATH=str(python.parent), TERSER_HOME=tmp, TOKENECONOMIZER_HOME=tmp)
            self.assertTrue(codex_hooks.equivalent_command(original + suffix, replacement + suffix, tmp, env))
            self.assertFalse(codex_hooks.equivalent_command(original + suffix, replacement + " -I" + suffix, tmp, env))
            self.assertFalse(codex_hooks.equivalent_command(original + suffix, "unrelated" + suffix, tmp, env))
            other = Path(tmp) / original
            other.write_text("different file", encoding="utf-8")
            self.assertFalse(codex_hooks.equivalent_command(original + suffix, "./" + original + suffix, tmp, env))
            before = subprocess.run([str(python), *args], cwd=tmp, capture_output=True, timeout=8)
            after = subprocess.run([replacement, *args], cwd=tmp, capture_output=True, timeout=8)
            self.assertEqual((before.stdout, before.stderr, before.returncode),
                             (after.stdout, after.stderr, after.returncode))
            self.assertEqual(after.returncode, 7)

            config.ensure_vendor_paths()
            from tokeneconomizer.microskills.envfacts import EnvFact, EnvFactsDistiller
            from tokeneconomizer.plugin import paths

            repo = paths.repo_identity(tmp, env)
            state = Path(paths.repo_state_dir(repo, "codex", env))
            mechanisms = str(state / "mechanisms")
            distiller = EnvFactsDistiller(repo.label)
            fact = EnvFact("python-fixture", "form_repair", True, {"a": original, "b_counts": {replacement: 4}})
            fact.life.state, fact.life.fire_den, fact.life.fire_num = "ACTIVE", 4, 4
            distiller.facts[fact.id] = fact
            distiller.save(mechanisms)

            cfg = dict(config.DEFAULT_CONFIG, codex_form_repair=True)
            payload = {"cwd": tmp, "tool_name": "Bash", "tool_input": {"command": original + suffix}}
            hook = unified_hook.handle_pre_tool_use("codex", payload, cfg, env)
            self.assertEqual(hook["hookSpecificOutput"]["updatedInput"]["command"], replacement + suffix)
            fact.params["b_counts"] = {replacement + " -I": 4}
            distiller.save(mechanisms)
            self.assertIsNone(unified_hook.handle_pre_tool_use("codex", payload, cfg, env))

            (state / "injection.json").write_text('{"rendered":"- Use the pinned Python interpreter."}', encoding="utf-8")
            self.assertEqual(session_brief._load_microskills(tmp, "codex", env), ["- Use the pinned Python interpreter."])

    def test_packaged_codex_launcher_and_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            market = install.build_codex_marketplace(base / "marketplace [fixture]", use_native=False)
            root = market / "terser"
            manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
            hooks = json.loads((root / manifest["hooks"]).read_text(encoding="utf-8"))["hooks"]
            pre = hooks["PreToolUse"][0]["matcher"]
            post = hooks["PostToolUse"][0]["matcher"]
            self.assertRegex("mcp__plugin_terser_terser-index__order", pre)
            self.assertRegex("apply_patch", post)
            self.assertIsNone(re.search(pre, "update_plan"))
            self.assertIsNone(re.search(post, "update_plan"))
            hook = hooks["SubagentStart"][0]["hooks"][0]
            if os.name == "nt":
                command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", hook["commandWindows"]]
            else:
                # A plain checkout can carry mode 0644. Invoking sh must still work.
                (root / "scripts" / "terse-hook").chmod(0o644)
                command = ["/bin/sh", "-c", hook["command"]]
            env = {k: v for k, v in os.environ.items() if not k.startswith("TERSER_")}
            env.update(PLUGIN_ROOT=str(root), TERSER_HOME=str(base / "state"), TERSER_PYTHON_WORKER="0")
            result = subprocess.run(
                command, cwd=base, env=env, capture_output=True, text=True, encoding="utf-8", timeout=10, check=True,
                input=json.dumps({"session_id": "hook-smoke", "agent_id": "fixture", "cwd": str(base)}),
            )
            self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"], "SubagentStart")
            self.assertEqual(codex_doctor._probe_mcp(root, str(base)), 6)

    @unittest.skipUnless(sys.version_info >= (3, 11), "TOML validation requires Python 3.11")
    def test_migration_removes_only_installer_owned_entries(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_HOME": tmp}):
            path = Path(tmp)
            hooks = path / "hooks.json"
            manifest_script = ROOT / "runtime" / "unified_hook.py"
            known = {"type": "command", "command": f'{sys.executable} -E -s -X utf8 "{manifest_script}" --host codex --event PreToolUse'}
            unrelated = {"type": "command", "command": "echo unrelated-terser-check"}
            hooks.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [known, unrelated]}]}}), encoding="utf-8")
            cfg = path / "config.toml"
            unmanaged = '[mcp_servers.terser-index]\ncommand = "unmanaged"\n\n'
            managed = (
                '# BEGIN terser\n[mcp_servers.terser-index]\ncommand = "python"\n'
                f'args = [{json.dumps(str(ROOT / "runtime" / "index_mcp.py"))}]\n# END terser\n'
            )
            cfg.write_text(unmanaged + managed, encoding="utf-8")
            install._migrate_codex_config()
            self.assertEqual(cfg.read_text(encoding="utf-8"), unmanaged)
            entries = json.loads(hooks.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
            self.assertEqual(entries, [{"matcher": "Bash", "hooks": [unrelated]}])
            self.assertEqual(install._strip_legacy_codex_hooks(hooks.read_text(encoding="utf-8")), hooks.read_text(encoding="utf-8"))

    @unittest.skipUnless(sys.version_info >= (3, 11), "TOML validation requires Python 3.11")
    def test_invalid_codex_configuration_leaves_both_files_intact(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_HOME": tmp}):
            path = Path(tmp)
            script = ROOT / "runtime" / "unified_hook.py"
            text = json.dumps({"hooks": {"PreToolUse": [{"hooks": [
                {"type": "command", "command": f'{sys.executable} -E -s -X utf8 "{script}" --host codex --event PreToolUse'},
            ]}]}})
            hooks = path / "hooks.json"
            hooks.write_text(text, encoding="utf-8")
            (path / "config.toml").write_text("[broken\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                install._migrate_codex_config()
            self.assertEqual(hooks.read_text(encoding="utf-8"), text)
