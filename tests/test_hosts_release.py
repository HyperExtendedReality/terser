"""opencode / Cursor host adapters, their installers, and release asset mapping."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("TERSER_PYTHON_WORKER", "0")

from runtime import host_adapters, host_install, unified_hook  # noqa: E402


def _load_installer():
    spec = importlib.util.spec_from_file_location("terser_install", ROOT / "scripts" / "install.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


install = _load_installer()

NOISY = "\n".join(
    [f"\x1b[33mINFO tick {i}\x1b[0m" for i in range(40)]
    + ["2026-11-24T12:00:01 INFO waiting for server"] * 50
    + ["FAILED test_api.py::test_timeout"]
)


class _HostCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="terser-hosts-"))
        self.env = {"TERSER_HOME": str(self.tmp / "state")}
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class OpencodeAdapterTests(_HostCase):
    def test_bash_after_hook_compresses_output(self) -> None:
        payload = {"tool": "bash", "sessionID": "oc-1", "callID": "c1",
                   "args": {"command": "pytest"}, "output": NOISY, "directory": str(self.repo)}
        out = unified_hook.dispatch("tool.execute.after", payload, host_hint="opencode", env=self.env)
        self.assertIn("output", out)
        self.assertLess(len(out["output"]), len(NOISY) * 0.7)
        self.assertIn("FAILED test_api.py::test_timeout", out["output"])

    def test_tool_name_and_arg_translation(self) -> None:
        self.assertEqual(host_adapters._translate_tool("opencode", "bash", {"command": "ls"}), ("Bash", {"command": "ls"}))
        self.assertEqual(host_adapters._translate_tool("opencode", "read", {"filePath": "a.py"}), ("Read", {"file_path": "a.py"}))
        self.assertEqual(host_adapters._translate_tool("opencode", "read", {"path": "a.py"}), ("Read", {"file_path": "a.py"}))
        self.assertEqual(
            host_adapters._translate_tool("opencode", "edit", {"filePath": "a.py", "oldString": "x", "newString": "y"}),
            ("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}),
        )
        self.assertEqual(host_adapters._translate_tool("opencode", "grep", {"pattern": "foo", "include": "*.py"}),
                         ("Grep", {"pattern": "foo", "glob": "*.py"}))
        # Ranged reads (offset base unconfirmed) and unknown tools pass through untouched.
        self.assertIsNone(host_adapters._translate_tool("opencode", "read", {"filePath": "a.py", "offset": 0, "limit": 5}))
        self.assertIsNone(host_adapters._translate_tool("opencode", "todowrite", {}))

    def test_large_read_is_blocked_with_outline(self) -> None:
        big = self.repo / "big.py"
        big.write_text("".join(f"def f{i}():\n    return {i}\n\n" for i in range(400)), encoding="utf-8")
        payload = {"tool": "read", "sessionID": "oc-2", "args": {"filePath": str(big)}, "directory": str(self.repo)}
        out = unified_hook.dispatch("tool.execute.before", payload, host_hint="opencode", env=self.env)
        self.assertIn("block", out)
        self.assertIn("big.py", out["block"])

    def test_unknown_tool_and_session_events_are_silent(self) -> None:
        self.assertEqual(unified_hook.dispatch("tool.execute.before", {"tool": "todowrite", "args": {}},
                                               host_hint="opencode", env=self.env), {})
        self.assertEqual(unified_hook.dispatch("session.idle", {"sessionID": "oc-3"}, host_hint="opencode", env=self.env), {})

    def test_session_start_returns_context_shape(self) -> None:
        out = unified_hook.dispatch("SessionStart", {"sessionID": "oc-4", "directory": str(self.repo)},
                                    host_hint="opencode", env=dict(self.env, TERSER_AUTO_INDEX="0"))
        self.assertTrue(set(out) <= {"context"})

    def test_plugin_template_renders(self) -> None:
        text = host_install.render_opencode_plugin("/usr/bin/python3")
        self.assertIn('const PYTHON = "/usr/bin/python3";', text)
        self.assertIn(json.dumps(str(ROOT / "runtime" / "unified_hook.py")), text)
        self.assertNotIn("__TERSER_", text)
        for hook in ('"tool.execute.before"', '"tool.execute.after"', '"--host", "opencode"'):
            self.assertIn(hook, text)


class CursorAdapterTests(_HostCase):
    def _base(self, event: str, **extra):
        return {"hook_event_name": event, "conversation_id": "cur-1", "generation_id": "g1",
                "workspace_roots": [str(self.repo)], "cursor_version": "1.7", **extra}

    def test_pre_tool_use_defaults_to_allow(self) -> None:
        out = unified_hook.dispatch("preToolUse", self._base("preToolUse", tool_name="Shell", tool_input={"command": "echo hi"}),
                                    host_hint="cursor", env=self.env)
        self.assertEqual(out.get("permission"), "allow")
        out = unified_hook.dispatch("preToolUse", self._base("preToolUse", tool_name="Delete", tool_input={"path": "x"}),
                                    host_hint="cursor", env=self.env)
        self.assertEqual(out, {"permission": "allow"})

    def test_subagent_start_always_answers(self) -> None:
        out = unified_hook.dispatch("subagentStart", self._base("subagentStart", subagent_id="s1"), host_hint="cursor", env=self.env)
        self.assertEqual(out, {"permission": "allow"})

    def test_large_read_denied_with_agent_message(self) -> None:
        big = self.repo / "big.py"
        big.write_text("".join(f"def f{i}():\n    return {i}\n\n" for i in range(400)), encoding="utf-8")
        out = unified_hook.dispatch("preToolUse", self._base("preToolUse", tool_name="Read", tool_input={"file_path": str(big)}),
                                    host_hint="cursor", env=self.env)
        self.assertEqual(out["permission"], "deny")
        self.assertIn("big.py", out["agent_message"])
        self.assertNotIn("\n", out["user_message"])

    def test_post_tool_use_never_rewrites_shell_output(self) -> None:
        out = unified_hook.dispatch("postToolUse", self._base("postToolUse", tool_name="Shell", tool_input={"command": "pytest"},
                                                               tool_output=NOISY), host_hint="cursor", env=self.env)
        self.assertNotIn("updated_mcp_tool_output", out)
        self.assertTrue(set(out) <= {"additional_context"})

    def test_stop_never_sends_followup(self) -> None:
        out = unified_hook.dispatch("stop", self._base("stop", status="completed", loop_count=0), host_hint="cursor", env=self.env)
        self.assertNotIn("followup_message", out)

    def test_adapter_failure_fails_open(self) -> None:
        def boom(*_a, **_k):
            raise RuntimeError("x")

        self.assertEqual(host_adapters.dispatch_foreign("cursor", "preToolUse", {"tool_name": "Read", "tool_input": {"file_path": "a"}},
                                                        self.env, boom), {"permission": "allow"})
        self.assertEqual(host_adapters.dispatch_foreign("opencode", "tool.execute.before",
                                                        {"tool": "bash", "args": {"command": "ls"}}, self.env, boom), {})


class HostInstallTests(_HostCase):
    def test_cursor_hooks_merge_keeps_user_hooks_and_is_idempotent(self) -> None:
        user = {"version": 1, "hooks": {"afterFileEdit": [{"command": "fmt.sh"}], "stop": [{"command": "notify.sh"}]}}
        once = host_install.merge_cursor_hooks(user, "/py")
        twice = host_install.merge_cursor_hooks(once, "/py")
        self.assertEqual(once, twice)
        self.assertEqual(twice["hooks"]["afterFileEdit"], [{"command": "fmt.sh"}])
        self.assertEqual(len(twice["hooks"]["stop"]), 2)
        self.assertEqual(set(host_install.CURSOR_EVENTS) - set(twice["hooks"]), set())
        removed = host_install.merge_cursor_hooks(twice, "", remove=True)
        self.assertEqual(removed["hooks"], user["hooks"])

    def test_cursor_command_quoting(self) -> None:
        posix = host_install.cursor_hook_command("/usr/bin/python3", "preToolUse", windows=False)
        self.assertTrue(posix.startswith('"/usr/bin/python3" -E -s -X utf8 "'))
        self.assertTrue(posix.endswith("--host cursor --event preToolUse"))
        win = host_install.cursor_hook_command(r"C:\Py\python.exe", "stop", windows=True)
        self.assertTrue(win.startswith('& "C:\\Py\\python.exe"'))

    def test_install_and_uninstall_round_trip(self) -> None:
        env = {"OPENCODE_CONFIG_DIR": str(self.tmp / "oc"), "CURSOR_HOME": str(self.tmp / "cur")}
        cur = self.tmp / "cur"
        cur.mkdir()
        (cur / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
        host_install.install_opencode("/py", env=env)
        host_install.install_cursor("/py", env=env)
        self.assertTrue((self.tmp / "oc" / "plugins" / "terser.js").is_file())
        mcp = json.loads((cur / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(set(mcp["mcpServers"]), {"other", "terser-index"})
        host_install.uninstall_opencode(env=env)
        host_install.uninstall_cursor(env=env)
        self.assertFalse((self.tmp / "oc" / "plugins" / "terser.js").exists())
        mcp = json.loads((cur / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(set(mcp["mcpServers"]), {"other"})
        self.assertEqual(json.loads((cur / "hooks.json").read_text(encoding="utf-8"))["hooks"], {})

    def test_malformed_user_json_is_never_overwritten(self) -> None:
        cur = self.tmp / "cur"
        cur.mkdir()
        (cur / "hooks.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            host_install.install_cursor("/py", env={"CURSOR_HOME": str(cur)})
        self.assertEqual((cur / "hooks.json").read_text(encoding="utf-8"), "{not json")


class ReleaseAssetTests(unittest.TestCase):
    CASES = {
        ("linux", "x86_64"): "terser-fast-linux-x86_64",
        ("linux", "amd64"): "terser-fast-linux-x86_64",
        ("linux", "aarch64"): "terser-fast-linux-aarch64",
        ("linux", "arm64"): "terser-fast-linux-aarch64",
        ("darwin", "x86_64"): "terser-fast-macos-x86_64",
        ("darwin", "arm64"): "terser-fast-macos-aarch64",
        ("win32", "AMD64"): "terser-fast-windows-x86_64.exe",
        ("win32", "ARM64"): "terser-fast-windows-aarch64.exe",
    }

    def test_asset_names(self) -> None:
        for (plat, arch), want in self.CASES.items():
            self.assertEqual(install._release_asset_name(plat, arch), want, (plat, arch))
        self.assertIsNone(install._release_asset_name("linux", "riscv64"))
        self.assertIsNone(install._release_asset_name("freebsd", "x86_64"))

    def test_release_matrix_matches_installer(self) -> None:
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        artifacts = set(re.findall(r"artifact:\s*(terser-fast-\S+)", text))
        self.assertEqual(len(artifacts), 6)
        self.assertEqual(artifacts, set(install._RELEASE_ASSETS.values()))
        self.assertIn("sha256sum terser-fast-* > SHA256SUMS", text)

    def test_checksum_fails_closed(self) -> None:
        orig = install._expected_sha256
        try:
            install._expected_sha256 = lambda _a: None
            self.assertFalse(install._checksum_ok("terser-fast-linux-aarch64", b"x"))

            def boom(_a):
                raise OSError("offline")

            install._expected_sha256 = boom
            self.assertFalse(install._checksum_ok("terser-fast-linux-aarch64", b"x"))
            install._expected_sha256 = lambda _a: "0" * 64
            self.assertFalse(install._checksum_ok("terser-fast-linux-aarch64", b"x"))
            import hashlib

            install._expected_sha256 = lambda _a: hashlib.sha256(b"x").hexdigest()
            self.assertTrue(install._checksum_ok("terser-fast-linux-aarch64", b"x"))
        finally:
            install._expected_sha256 = orig


if __name__ == "__main__":
    unittest.main()
