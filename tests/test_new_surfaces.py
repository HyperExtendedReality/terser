"""Tests for command filters, .contextignore, lazy MCP docs, Code Mode, web archive,
activity-aware compaction, extra handlers, lossy JSON, and repository analysis."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime import (  # noqa: E402
    activity,
    code_mode,
    command_handlers,
    index_analysis,
    index_mcp,
    mcp_proxy,
    output_compressor,
    unified_hook,
    user_filters,
)

HAS_GIT = shutil.which("git") is not None


class _Home(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name) / "home"
        self.home.mkdir()
        self.ws = Path(self._td.name) / "ws"
        self.ws.mkdir()
        self.env = {**os.environ, "TERSER_HOME": str(self.home)}
        user_filters._cache.clear()
        self._patch = mock.patch.dict(os.environ, {"TERSER_HOME": str(self.home)})
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._td.cleanup()


class TomlParserTests(unittest.TestCase):
    def test_minimal_parser_matches_shapes(self) -> None:
        text = textwrap.dedent('''
            # comment
            [[filter]]
            name = "tool"   # trailing
            match = "^tool\\\\s+build"
            drop = [
              "^INFO ",  # one
              '^DEBUG',
            ]
            [exclude]
            commands = ["cat *"]
            [tool.uv.workspace]
            members = ["pkgs/*"]
        ''')
        with mock.patch.dict(sys.modules, {"tomllib": None}):
            data = user_filters.parse_toml(text)
        self.assertEqual(data["filter"][0]["name"], "tool")
        self.assertEqual(data["filter"][0]["match"], "^tool\\s+build")
        self.assertEqual(data["filter"][0]["drop"], ["^INFO ", "^DEBUG"])
        self.assertEqual(data["exclude"]["commands"], ["cat *"])
        self.assertEqual(data["tool"]["uv"]["workspace"]["members"], ["pkgs/*"])


class CommandFilterTests(_Home):
    def _write(self, body: str) -> None:
        (self.home / "command-filters.toml").write_text(textwrap.dedent(body), encoding="utf-8")

    def test_drop_keeps_signal_lines_and_counts(self) -> None:
        self._write('''
            [[filter]]
            name = "mytool"
            match = "^mytool"
            drop = ["^\\\\[info\\\\]"]
        ''')
        text = "\n".join(["[info] step %d" % i for i in range(30)] + ["[info] error: disk full", "done"])
        out = user_filters.apply_filters("mytool build", text, env=self.env)
        self.assertIn("[info] error: disk full", out)
        self.assertIn("done", out)
        self.assertIn("30 mytool line(s) omitted", out)
        self.assertNotIn("step 3\n", out)

    def test_failed_non_test_command_untouched(self) -> None:
        self._write('[[filter]]\nmatch = "^mytool"\ndrop = ["^x"]\n')
        text = "x\n" * 50
        self.assertEqual(user_filters.apply_filters("mytool", text, returncode=1, env=self.env), text)

    def test_exclude_skips_compression(self) -> None:
        self._write('[exclude]\ncommands = ["cat *.json"]\n')
        raw = "\x1b[31m" + ("line\n" * 400)
        out, stats = output_compressor.compress_output(raw, command="cat data.json", env=self.env)
        self.assertEqual(out, raw)
        self.assertTrue(stats.get("excluded"))

    def test_handler_alias_and_project_scope(self) -> None:
        proj = self.ws / ".terser"
        proj.mkdir()
        (proj / "command-filters.toml").write_text('[[filter]]\nmatch = "^runtests"\nhandler = "pytest"\n', encoding="utf-8")
        text = "\n".join([f"tests/test_a.py::test_{i} PASSED" for i in range(40)] + ["1 failed, 40 passed"])
        out = user_filters.apply_filters("runtests -q", text, workspace=str(self.ws), env=self.env)
        self.assertLess(len(out), len(text))
        self.assertIn("1 failed", out)
        self.assertFalse(user_filters.user_filter_allows_wrap("runtests -q", env=self.env))

    def test_bad_regex_is_skipped(self) -> None:
        self._write('[[filter]]\nmatch = "("\ndrop = ["x"]\n')
        self.assertIsNone(user_filters.matching_filter("anything", env=self.env))


class ContextIgnoreTests(_Home):
    def test_patterns_negation_and_dirs(self) -> None:
        (self.ws / ".contextignore").write_text("*.secret\nfixtures/\n!fixtures/keep.json\n/top.txt\n", encoding="utf-8")
        ws = str(self.ws)
        self.assertTrue(user_filters.ignored_by(str(self.ws / "a" / "b.secret"), ws, self.env))
        self.assertTrue(user_filters.ignored_by(str(self.ws / "fixtures" / "big.json"), ws, self.env))
        self.assertIsNone(user_filters.ignored_by(str(self.ws / "fixtures" / "keep.json"), ws, self.env))
        self.assertTrue(user_filters.ignored_by(str(self.ws / "top.txt"), ws, self.env))
        self.assertIsNone(user_filters.ignored_by(str(self.ws / "sub" / "top.txt"), ws, self.env))

    def test_global_file_applies(self) -> None:
        (self.home / ".contextignore").write_text("*.pem\n", encoding="utf-8")
        self.assertTrue(user_filters.ignored_by(str(self.ws / "k.pem"), str(self.ws), self.env))

    def test_read_hook_denies(self) -> None:
        (self.ws / ".contextignore").write_text("*.lock\n", encoding="utf-8")
        target = self.ws / "poetry.lock"
        target.write_text("x" * 100, encoding="utf-8")
        payload = {"tool_name": "Read", "tool_input": {"file_path": str(target)}, "cwd": str(self.ws), "session_id": "s1"}
        out = unified_hook.dispatch("PreToolUse", payload, host_hint="claude", env=self.env)
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "deny")
        self.assertIn(".contextignore", hso["permissionDecisionReason"])

    def test_index_skips_ignored_files(self) -> None:
        (self.ws / ".contextignore").write_text("gen/\n", encoding="utf-8")
        (self.ws / "gen").mkdir()
        (self.ws / "gen" / "a.py").write_text("def a():\n    pass\n", encoding="utf-8")
        (self.ws / "b.py").write_text("def b():\n    pass\n", encoding="utf-8")
        self.assertEqual(index_mcp._list_source_files(self.ws), ["b.py"])


class HandlerTests(unittest.TestCase):
    def test_xcodebuild_keeps_errors(self) -> None:
        lines = []
        for i in range(20):
            lines += [f"CompileSwift normal arm64 /src/F{i}.swift", "    cd /proj", "    /usr/bin/swiftc -c F.swift"]
        lines += ["/src/A.swift:3:5: error: cannot find 'x' in scope", "** BUILD FAILED **"]
        out = command_handlers.compress("xcodebuild -scheme App test", "\n".join(lines), returncode=65)
        self.assertIn("error: cannot find", out)
        self.assertIn("BUILD FAILED", out)
        self.assertIn("20 xcodebuild build-step", out)

    def test_logcat_drops_debug_and_collapses(self) -> None:
        rows = [f"11-27 10:00:{i % 60:02d}.000  100  200 D Chatty: tick {i}" for i in range(250)]
        rows += ["11-27 10:01:00.000  100  200 E Crash: FATAL EXCEPTION: main"] * 3
        out = command_handlers.compress("adb logcat -d", "\n".join(rows))
        self.assertIn("FATAL EXCEPTION", out)
        self.assertIn("x2 more identical", out)
        self.assertIn("250 verbose/debug logcat", out)


class LossyJsonTests(_Home):
    def test_opt_in_only(self) -> None:
        doc = json.dumps({"items": [{"id": i, "trace_id": "t" * 30, "note": None, "body": "y" * 900} for i in range(5)]}, indent=2)
        off, _ = output_compressor.compress_output(doc, command="curl api", env=self.env, json_lossy=False)
        self.assertIn("trace_id", off)
        on, stats = output_compressor.compress_output(doc, command="curl api", env=self.env, json_lossy=True)
        self.assertNotIn("trace_id", on)
        self.assertIn("lossy JSON view", on)
        self.assertTrue(stats["archive_id"])


class LazyProxyTests(unittest.TestCase):
    def test_lazy_tools_and_help(self) -> None:
        originals: dict = {}
        res = {"tools": [{"name": "search", "description": "Search the issues. Supports many filters and paging.\n\nLong docs.",
                          "inputSchema": {"type": "object", "properties": {"q": {"type": "string", "description": "query text"}}}}]}
        slim = mcp_proxy.lazy_tools_list(res, originals)
        tool = slim["tools"][0]
        self.assertEqual(tool["description"], "Search the issues.")
        self.assertNotIn("description", tool["inputSchema"]["properties"]["q"])
        self.assertEqual(slim["tools"][-1]["name"], mcp_proxy.HELP_TOOL)
        helped = mcp_proxy.tool_help_result(originals, "search")
        self.assertIn("Long docs", helped["content"][0]["text"])
        self.assertTrue(mcp_proxy.tool_help_result(originals, "nope")["isError"])

    def test_proxy_answers_help_locally(self) -> None:
        p = mcp_proxy._Proxy(["x"], lazy_tools=True)
        p.originals["a"] = {"name": "a", "description": "Full A."}
        buf = io.BytesIO()
        with mock.patch.object(sys, "stdout", mock.Mock(buffer=buf)):
            consumed = p._answer_locally({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                          "params": {"name": mcp_proxy.HELP_TOOL, "arguments": {"tool": "a"}}})
        self.assertTrue(consumed)
        reply = json.loads(buf.getvalue())
        self.assertEqual(reply["id"], 7)
        self.assertIn("Full A.", reply["result"]["content"][0]["text"])
        self.assertFalse(p._answer_locally({"id": 8, "method": "tools/call", "params": {"name": "a"}}))


class WebArchiveTests(_Home):
    def test_large_page_archived_with_outline(self) -> None:
        page = "\n".join(f"# Section {i}\n" + ("text " * 60 + "\n") * 5 for i in range(30))
        out = output_compressor.archive_web_page(page, source="https://ex.test/doc", threshold=2000, env=self.env)
        self.assertIsNotNone(out)
        self.assertIn("Sections (archive line numbers)", out)
        self.assertIn("Section 29", out)
        self.assertLess(len(out), len(page))
        self.assertIsNone(output_compressor.archive_web_page("short", threshold=2000, env=self.env))

    def test_fetch_and_index_formats(self) -> None:
        from runtime import web_fetch

        html = "<html><body>" + "".join(f"<h2>Part {i}</h2><p>{'alpha ' * 80}</p>" for i in range(20)) + "<h2>Install</h2><p>pip install widget</p></body></html>"

        class _Resp(io.BytesIO):
            headers = mock.Mock(get=lambda k, d=None: "text/html; charset=utf-8", get_content_charset=lambda: "utf-8")

            def geturl(self) -> str:
                return "https://ex.test/p"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch("urllib.request.urlopen", return_value=_Resp(html.encode())):
            res = web_fetch.fetch_and_index({"url": "https://ex.test/p", "query": "pip install"}, env=self.env)
        self.assertNotIn("error", res)
        text = web_fetch.format_response(res)
        self.assertIn("pip install widget", text)
        self.assertIn("Install", text)
        self.assertEqual(web_fetch.fetch_and_index({"url": "file:///etc/passwd"}, env=self.env).get("error"), "Provide an http(s) `url`.")


class ActivityTests(unittest.TestCase):
    def test_modes(self) -> None:
        self.assertEqual(activity.classify_mode([("Edit", "a.py")] * 4), "code")
        self.assertEqual(activity.classify_mode([("Bash", "pytest -x"), ("Edit", "a.py"), ("Bash", "pytest -x")], errors=1), "debug")
        self.assertEqual(activity.classify_mode([("Bash", "git diff HEAD~1"), ("Bash", "git log -5"), ("Read", "a.py")]), "review")
        self.assertEqual(activity.classify_mode([("Bash", "kubectl get pods"), ("Bash", "kubectl describe pod x"), ("Bash", "helm list")]), "infra")
        self.assertEqual(activity.classify_mode([("Read", "a"), ("Grep", "b"), ("Glob", "c"), ("Read", "d")]), "research")
        self.assertEqual(activity.classify_mode([]), "general")

    def test_decisions(self) -> None:
        out: list[str] = []
        activity.collect_decisions("Let's use SQLite instead of Postgres for the cache. Thanks!", out, source="user")
        activity.collect_decisions("Let me read the file first.", out, source="assistant")
        activity.collect_decisions("I decided to keep the old API rather than break callers.", out, source="assistant")
        self.assertEqual(len(out), 2)
        self.assertTrue(out[0].startswith("(user) Let's use SQLite"))
        for i in range(20):
            activity.collect_decisions(f"Always use option number {i} for builds.", out, source="user")
        self.assertEqual(len(out), activity.MAX_DECISIONS)

    def test_compact_instructions_include_mode_and_decisions(self) -> None:
        from runtime import analytics

        with tempfile.TemporaryDirectory() as td:
            tp = Path(td) / "t.jsonl"
            recs = [
                {"type": "user", "message": {"content": "Don't touch the public API; use a feature flag instead."}},
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": "Edit", "input": {"file_path": f"/r/f{i}.py"}} for i in range(4)]}},
            ]
            tp.write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
            text = analytics.compact_instructions({"transcript_path": str(tp)})
        self.assertIn("Current activity: code", text)
        self.assertIn("CRITICAL DECISIONS", text)
        self.assertIn("feature flag", text)


class LanguageTests(unittest.TestCase):
    def test_extended_extractors(self) -> None:
        cases = {
            "x.ex": ("defmodule Foo do\n  def bar(x) do\n    x\n  end\nend\n", {"Foo", "bar"}),
            "x.tf": ('resource "aws_s3_bucket" "b" {\n}\n', {"aws_s3_bucket.b"}),
            "x.proto": ("message User {\n}\nservice Api {\n  rpc Get (A) returns (B);\n}\n", {"User", "Api", "Get"}),
            "x.vue": ("<script>\nexport function setup() {\n}\n</script>\n", {"setup"}),
            "x.cu": ("__global__ void kern(int* a) {\n  if (a) {\n  }\n}\n", {"kern"}),
        }
        for rel, (src, names) in cases.items():
            got = {s["name"] for s in index_mcp._extract_generic_symbols(rel, src)}
            self.assertEqual(got, names, rel)


@unittest.skipUnless(HAS_GIT, "git required")
class RepoTests(_Home):
    def setUp(self) -> None:
        super().setUp()
        r = self.ws
        (r / "src").mkdir()
        (r / "tests").mkdir()
        (r / "src" / "shapes.py").write_text(textwrap.dedent('''
            class Shape:
                def area(self):
                    return 0


            class Square(Shape):
                def area(self):
                    return self.side * self.side


            def helper_one(items):
                total = 0
                for item in items:
                    if item > 10:
                        total += item * 2
                    else:
                        total -= item
                return total


            def helper_two(values):
                total = 0
                for item in values:
                    if item > 10:
                        total += item * 2
                    else:
                        total -= item
                return total
        ''').lstrip(), encoding="utf-8")
        (r / "src" / "ui.py").write_text("from core import thing\n\ndef render():\n    return thing()\n", encoding="utf-8")
        (r / "src" / "core.py").write_text("def thing():\n    return 1\n", encoding="utf-8")
        (r / "tests" / "test_shapes.py").write_text("from src.shapes import Square\n\ndef test_sq():\n    assert Square\n", encoding="utf-8")
        (r / "package.json").write_text(json.dumps({"workspaces": ["src"]}), encoding="utf-8")
        cfg = r / "config.json"
        cfg.write_text(json.dumps({"dependencies": {"a": "1.0", "b": "2.0"}, "items": [{"name": "first"}]}), encoding="utf-8")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        res = index_mcp.index_folder(str(r), env=self.env)
        self.slug = res.get("repo") or index_mcp.repo_slug_for_path(str(r), env=self.env)
        self.assertTrue(self.slug)

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.ws), *args], check=True, capture_output=True)

    def act(self, action: str, **args) -> dict:
        return index_mcp.dispatch_action(action, {"repo": self.slug, **args}, env=self.env)

    def text(self, action: str, **args) -> str:
        return index_mcp.format_mcp_response(action, self.act(action, **args))

    def test_analysis_actions(self) -> None:
        self.assertIn("src/shapes.py", self.text("get_hotspots"))
        untested = self.text("get_untested_symbols")
        self.assertIn("helper_one", untested)
        self.assertNotIn("Square L", untested)
        self.assertIn("Square", self.text("get_class_hierarchy", symbol="Shape"))
        self.assertIn("Square", self.text("find_implementations", symbol="Shape"))
        sim = self.text("find_similar_symbols", threshold=0.5, min_lines=5)
        self.assertIn("helper_one", sim)
        self.assertIn("helper_two", sim)
        ws = self.text("list_workspaces")
        self.assertIn("src (npm", ws)
        layers = self.act("get_layer_violations", layers=[{"name": "ui", "paths": ["src/ui.py"]}, {"name": "core", "paths": ["src/core.py"]}])
        self.assertEqual(layers["count"], 0)
        inverted = self.act("get_layer_violations", layers=[{"name": "core", "paths": ["src/core.py"]}, {"name": "ui", "paths": ["src/ui.py"]}])
        self.assertEqual(inverted["count"], 1)
        scoped = self.act("find_dead_code", path_prefix="tests")
        self.assertNotIn("helper_one", json.dumps(scoped))

    def test_symbol_diff_and_risk(self) -> None:
        p = self.ws / "src" / "core.py"
        p.write_text("def thing():\n    return 2\n", encoding="utf-8")
        index_mcp.reindex_file(str(p), env=self.env)
        diff = self.text("get_symbol_diff", symbol="thing")
        self.assertIn("-    return 1", diff)
        self.assertIn("+    return 2", diff)
        risk = self.text("get_pr_risk_profile")
        self.assertIn("thing", risk)
        self.assertRegex(risk, r"Risk (LOW|MEDIUM|HIGH)")

    def test_scip_import_drives_find_references(self) -> None:
        def varint(n: int) -> bytes:
            out = b""
            while True:
                b = n & 0x7F
                n >>= 7
                out += bytes([b | (0x80 if n else 0)])
                if not n:
                    return out

        def field(num: int, payload: bytes) -> bytes:
            return varint((num << 3) | 2) + varint(len(payload)) + payload

        sym = b"scip-python python pkg 0.1 `src.core`/thing()."
        occ = field(1, varint(0) + varint(4) + varint(9)) + field(2, sym) + varint(3 << 3) + varint(0)
        doc = field(1, b"src/ui.py") + field(2, occ)
        (self.ws / "index.scip").write_bytes(field(2, doc))
        self.assertEqual(index_analysis.scip_symbol_name(sym.decode()), "thing")
        self.assertIn("Imported 1", self.text("import_scip"))
        refs = self.act("find_references", symbol="thing")
        self.assertEqual(refs.get("source"), "scip")
        self.assertEqual(refs["references"], [{"file": "src/ui.py", "lines": [1]}])

    def test_code_mode_actions(self) -> None:
        q = self.text("code_query", path="config.json", select="dependencies.*")
        self.assertIn("dependencies.a\t1.0", q)
        self.assertIn("items[0].name\tfirst", self.text("code_query", path="config.json", select="items[0].name"))
        self.assertIn("dependencies: dict (2)", self.text("code_query", path="config.json", outline=True))

        s = self.text("code_search", all=["def "], none=["class "], glob="*.py")
        self.assertIn("src/core.py", s)
        self.assertNotIn("src/shapes.py", s)

        dry = self.text("code_edit", edits=[{"path": "src/core.py", "old": "return 1", "new": "return 3"}])
        self.assertIn("+    return 3", dry)
        self.assertIn("return 1", (self.ws / "src" / "core.py").read_text(encoding="utf-8"))
        bad = self.act("code_edit", apply=True, edits=[{"path": "src/core.py", "old": "return 1", "new": "return 3"},
                                                      {"path": "src/ui.py", "old": "missing text", "new": "x"}])
        self.assertIn("Nothing was written", bad["error"])
        self.assertIn("return 1", (self.ws / "src" / "core.py").read_text(encoding="utf-8"))
        ok = self.text("code_edit", apply=True, edits=[{"path": "src/core.py", "old": "return 1", "new": "return 3"}])
        self.assertIn("[terser-code-mode] edited:", ok)
        self.assertIn("return 3", (self.ws / "src" / "core.py").read_text(encoding="utf-8"))

    def test_run_script(self) -> None:
        script = "for h in t.grep(r'def ', '*.py'):\n    print(h.path, h.line)\nt.replace('src/core.py', 'return 1', 'return 5')\n"
        res = self.act("run_script", code=script)
        self.assertEqual(res["returncode"], 0, res)
        self.assertIn("src/core.py 1", res["output"])
        self.assertIn("dry run", res["output"])
        self.assertIn("return 1", (self.ws / "src" / "core.py").read_text(encoding="utf-8"))
        res = self.act("run_script", code=script, apply=True)
        self.assertEqual(res.get("edited"), [str((self.ws / "src" / "core.py").resolve())])
        self.assertIn("return 5", (self.ws / "src" / "core.py").read_text(encoding="utf-8"))
        boom = self.act("run_script", code="t.write('src/new.py', 'x = 1\\n')\nraise RuntimeError('stop')\n", apply=True)
        self.assertNotEqual(boom["returncode"], 0)
        self.assertFalse((self.ws / "src" / "new.py").exists())
        self.assertIn("nothing written", boom["output"])
        outside = self.act("run_script", code="t.write('../escape.txt', 'x')\n", apply=True)
        self.assertIn("outside the repo root", outside["output"])

    def test_code_mode_refreshes_read_cache(self) -> None:
        from runtime import read_funnel

        target = self.ws / "src" / "core.py"
        with mock.patch.object(read_funnel, "invalidate_on_edit") as inv:
            raw = json.dumps([{"type": "text", "text": f"ok\n[terser-code-mode] edited: {target}"}])
            unified_hook._seed_reads_from_index_output("s1", str(self.ws), raw, self.env)
        inv.assert_called_once()
        self.assertEqual(inv.call_args[0][1], str(target))

    def test_code_mode_off(self) -> None:
        env = dict(self.env, TERSER_CODE_MODE="0")
        res = index_mcp.dispatch_action("run_script", {"repo": self.slug, "code": "print(1)"}, env=env)
        self.assertIn("Code Mode is off", res["error"])


if __name__ == "__main__":
    unittest.main()
