"""Tests for the lossless search, edit, and token upgrades (round 5)."""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime import config, edit_assist, index_mcp, read_funnel, session_brief, unified_hook  # noqa: E402

os.environ.setdefault("TERSER_PYTHON_WORKER", "0")


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.env = {"TERSER_HOME": str(self.tmp / "state")}
        self.cfg = config.load_config(self.env)

    def tearDown(self) -> None:
        self._td.cleanup()


class IncrementalIndexTests(_Base):
    def _make_repo(self, n: int = 6) -> Path:
        repo = self.tmp / "repo"
        (repo / "pkg").mkdir(parents=True)
        for i in range(n):
            (repo / "pkg" / f"mod_{i}.py").write_text(
                f"def func_{i}(x):\n    return x + {i}\n\n\nclass Klass{i}:\n    def method(self):\n        return 'needle_{i}'\n",
                encoding="utf-8",
            )
        return repo

    def _symbol_rows(self, slug: str) -> list[tuple]:
        with closing(sqlite3.connect(str(index_mcp._db_path(slug, self.env)))) as conn:
            return sorted(conn.execute("SELECT id, name, file, start_line, end_line FROM symbols").fetchall())

    def test_reindex_reuses_unchanged_files_and_matches_full_rebuild(self) -> None:
        repo = self._make_repo()
        first = index_mcp.index_folder(str(repo), env=self.env)
        self.assertEqual(first["reparsed"], 6)
        second = index_mcp.index_folder(str(repo), env=self.env)
        self.assertEqual(second["reparsed"], 0)
        self.assertEqual(second["reused"], 6)

        target = repo / "pkg" / "mod_2.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n\ndef added_fn():\n    return 1\n", encoding="utf-8")
        _bump_mtime(target)
        (repo / "pkg" / "mod_5.py").unlink()
        third = index_mcp.index_folder(str(repo), env=self.env)
        self.assertEqual(third["reparsed"], 1)
        self.assertEqual(third["files"], 5)
        incremental = self._symbol_rows(first["repo"])

        # A from-scratch rebuild must produce exactly the same rows.
        index_mcp._db_path(first["repo"], self.env).unlink()
        index_mcp.index_folder(str(repo), env=self.env)
        self.assertEqual(incremental, self._symbol_rows(first["repo"]))
        self.assertTrue(any(r[1] == "added_fn" for r in incremental))

    def test_parallel_parse_matches_serial(self) -> None:
        repo = self._make_repo(8)
        serial = index_mcp._run_parse_jobs(repo, sorted(index_mcp._list_source_files(repo)), {"TERSER_INDEX_WORKERS": "1"})
        parallel = index_mcp._run_parse_jobs(repo, sorted(index_mcp._list_source_files(repo)), {"TERSER_INDEX_WORKERS": "2"})
        self.assertEqual(serial, parallel)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_gitignored_files_are_not_indexed(self) -> None:
        repo = self._make_repo(2)
        (repo / "generated").mkdir()
        (repo / "generated" / "big_gen.py").write_text("def generated_fn():\n    pass\n", encoding="utf-8")
        (repo / ".gitignore").write_text("generated/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        files = index_mcp._list_source_files(repo)
        self.assertIn("pkg/mod_0.py", files)  # untracked but not ignored -> still indexed
        self.assertNotIn("generated/big_gen.py", files)

    @unittest.skipUnless(shutil.which("git"), "git not installed")
    def test_nested_repo_files_stay_indexed(self) -> None:
        repo = self._make_repo(1)
        sub = repo / "third_party" / "lib"
        sub.mkdir(parents=True)
        (sub / "inner.py").write_text("def inner_fn():\n    pass\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(sub)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        files = index_mcp._list_source_files(repo)
        self.assertIn("third_party/lib/inner.py", files)
        self.assertIn("pkg/mod_0.py", files)

    def test_extractor_change_forces_full_reparse(self) -> None:
        repo = self._make_repo(3)
        index_mcp.index_folder(str(repo), env=self.env)
        self.assertEqual(index_mcp.index_folder(str(repo), env=self.env)["reparsed"], 0)
        orig = index_mcp._extractor_tag
        index_mcp._extractor_tag = lambda: "v999-ts"
        try:
            self.assertEqual(index_mcp.index_folder(str(repo), env=self.env)["reparsed"], 3)
            self.assertEqual(index_mcp.index_folder(str(repo), env=self.env)["reparsed"], 0)
        finally:
            index_mcp._extractor_tag = orig

    def test_search_text_prefilter_is_identical_to_full_scan(self) -> None:
        repo = self._make_repo()
        res = index_mcp.index_folder(str(repo), env=self.env)
        # File changed after indexing must still be found (stat mismatch -> always scanned).
        late = repo / "pkg" / "mod_1.py"
        late.write_text(late.read_text(encoding="utf-8") + "\n# NEEDLE_late marker\n", encoding="utf-8")
        _bump_mtime(late)

        queries = ["needle_3", "NEEDLE_LATE", "return x", "klass4", "zz-not-there", "ab"]
        with_fts = {q: index_mcp.dispatch_action("search_text", {"repo": res["repo"], "query": q}, env=self.env) for q in queries}
        self.assertEqual(len(with_fts["needle_3"]["matches"]), 1)
        self.assertEqual(with_fts["NEEDLE_LATE"]["matches"][0]["file"], "pkg/mod_1.py")
        self.assertEqual(len(with_fts["return x"]["matches"]), 6)

        with closing(sqlite3.connect(str(index_mcp._db_path(res["repo"], self.env)))) as conn:
            conn.execute("DROP TABLE content_fts")
            conn.commit()
        for q in queries:
            full = index_mcp.dispatch_action("search_text", {"repo": res["repo"], "query": q}, env=self.env)
            self.assertEqual(with_fts[q], full, q)

    def test_candidates_skip_only_verified_non_matching_files(self) -> None:
        repo = self._make_repo(3)
        res = index_mcp.index_folder(str(repo), env=self.env)
        files = index_mcp._list_source_files(repo)
        self.assertEqual(index_mcp._text_candidates(res["repo"], repo, files, "needle_2", env=self.env), ["pkg/mod_2.py"])
        self.assertEqual(index_mcp._text_candidates(res["repo"], repo, files, "xy", env=self.env), files)


class _FakeNode:
    def __init__(self, type_: str, start: int, end: int, src: bytes, name: str | None = None, children=None):
        self.type = type_
        self.start_point = (start, 0)
        self.end_point = (end, 0)
        self._name = name
        self._src = src
        self.named_children = children or []
        self.named_child_count = len(self.named_children)
        if name is not None:
            idx = src.find(name.encode())
            self._name_node = _NameNode(idx, idx + len(name))
        else:
            self._name_node = None
        self.start_byte = 0
        self.end_byte = 0

    def child_by_field_name(self, field: str):
        return self._name_node if field == "name" else None


class _NameNode:
    type = "identifier"
    named_children: list = []

    def __init__(self, sb: int, eb: int):
        self.start_byte = sb
        self.end_byte = eb

    def child_by_field_name(self, field: str):
        return None


class TreeSitterFallbackTests(unittest.TestCase):
    def tearDown(self) -> None:
        index_mcp._TS_PARSERS.clear()

    def test_regex_fallback_without_tree_sitter(self) -> None:
        index_mcp._TS_PARSERS["rust"] = index_mcp._TS_DISABLED
        syms = index_mcp._extract_generic_symbols("lib.rs", "pub fn alpha() {\n}\n\nstruct Beta {\n}\n")
        self.assertEqual([s["name"] for s in syms], ["alpha", "Beta"])

    def test_tree_sitter_spans_and_nesting(self) -> None:
        text = "impl Foo {\n    fn bar(&self) {\n        1\n    }\n}\nfn top() {}\n"
        src = text.encode()
        bar = _FakeNode("function_item", 1, 3, src, name="bar")
        impl = _FakeNode("impl_item", 0, 4, src, name=None, children=[_FakeNode("declaration_list", 0, 4, src, children=[bar])])
        impl._name_node = None
        impl.child_by_field_name = lambda f: _NameNode(5, 8) if f == "type" else None  # "Foo"
        top = _FakeNode("function_item", 5, 5, src, name="top")
        root = _FakeNode("source_file", 0, 6, src, children=[impl, top])

        class _Parser:
            def parse(self, _b):
                return type("T", (), {"root_node": root})()

        index_mcp._TS_PARSERS["rust"] = _Parser()
        syms = index_mcp._extract_treesitter_symbols("lib.rs", text)
        assert syms is not None
        got = [(s["qualified_name"], s["kind"], s["start_line"], s["end_line"]) for s in syms]
        self.assertEqual(got, [("Foo", "impl", 1, 5), ("Foo.bar", "fn", 2, 4), ("top", "fn", 6, 6)])


class VerifiedEditSeedingTests(_Base):
    def _file(self, name: str, text: str) -> Path:
        f = self.tmp / name
        f.write_bytes(text.encode("utf-8"))
        return f

    def test_formatter_change_after_edit_is_reported_not_hidden(self) -> None:
        f = self._file("fmt.py", "".join(f"x_{i} = {i}\n" for i in range(30)))
        self.assertIsNone(read_funnel.check_read("s1", str(f), cfg=self.cfg, env=self.env))
        # Model edits x_3; a formatter then also rewrites x_20 on disk.
        text = f.read_text(encoding="utf-8").replace("x_3 = 3\n", "x_3 = 33\n").replace("x_20 = 20\n", "x_20 = 20  # fmt\n")
        f.write_bytes(text.encode("utf-8"))
        read_funnel.seed_after_edit("s1", str(f), old_text="x_3 = 3\n", new_text="x_3 = 33\n", env=self.env)
        res = read_funnel.check_read("s1", str(f), cfg=self.cfg, env=self.env)
        assert res is not None
        self.assertNotIn("unchanged", res["reason"])
        self.assertIn("+x_20 = 20  # fmt", res["reason"])
        self.assertNotIn("x_3 = 33", res["reason"])  # the model's own edit is not echoed back

    def test_multiedit_seeds_t0_when_disk_matches(self) -> None:
        f = self._file("multi.py", "a = 1\nb = 2\nc = 3\n")
        read_funnel.check_read("s2", str(f), cfg=self.cfg, env=self.env)
        f.write_bytes(b"a = 10\nb = 2\nc = 30\n")
        read_funnel.seed_after_edit("s2", str(f), edits=[("a = 1\n", "a = 10\n", False), ("c = 3\n", "c = 30\n", False)], env=self.env)
        res = read_funnel.check_read("s2", str(f), cfg=self.cfg, env=self.env)
        assert res is not None
        self.assertIn("unchanged", res["reason"])

    def test_crlf_file_edit_with_lf_strings_still_verifies(self) -> None:
        f = self._file("crlf.py", "a = 1\r\nb = 2\r\n")
        read_funnel.check_read("s3", str(f), cfg=self.cfg, env=self.env)
        f.write_bytes(b"a = 1\r\nb = 22\r\n")
        read_funnel.seed_after_edit("s3", str(f), old_text="b = 2\n", new_text="b = 22\n", env=self.env)
        res = read_funnel.check_read("s3", str(f), cfg=self.cfg, env=self.env)
        assert res is not None
        self.assertIn("unchanged", res["reason"])

    def test_write_then_formatter_returns_exact_diff(self) -> None:
        f = self.tmp / "w.py"
        written = "def f():\n    return 1\n"
        f.write_bytes(b"def f():\n    return 1  # formatted\n")
        read_funnel.seed_after_write("s4", str(f), written=written, env=self.env)
        res = read_funnel.check_read("s4", str(f), cfg=self.cfg, env=self.env)
        assert res is not None
        self.assertIn("+    return 1  # formatted", res["reason"])

    def test_partial_coverage_multiedit_shifts_intervals(self) -> None:
        f = self._file("part.py", "".join(f"line_{i}\n" for i in range(1, 301)))
        self.assertIsNone(read_funnel.check_read("s5", str(f), start_line=1, end_line=50, cfg=self.cfg, env=self.env))
        text = f.read_text(encoding="utf-8").replace("line_10\n", "line_10\nextra_a\nextra_b\n")
        f.write_bytes(text.encode("utf-8"))
        read_funnel.seed_after_edit("s5", str(f), edits=[("line_10\n", "line_10\nextra_a\nextra_b\n", False)], env=self.env)
        res = read_funnel.check_read("s5", str(f), start_line=1, end_line=52, cfg=self.cfg, env=self.env)
        assert res is not None
        self.assertIn("unchanged", res["reason"])
        # Lines beyond the originally-read window were never seen and still pass through.
        self.assertIsNone(read_funnel.check_read("s5", str(f), start_line=200, end_line=210, cfg=self.cfg, env=self.env))


class SymbolDeltaTests(_Base):
    @staticmethod
    def _apply_regions(old: str, reason: str) -> str:
        """Rebuild the new file from the old text plus the symbol-delta blocks."""
        import re

        old_lines = old.splitlines()
        blocks = re.findall(r"^(old L(\d+)-L(\d+)|insert after old L(\d+)) -> now L(\d+)-L(\d+).*?:\n((?:\d+: .*\n?)+)", reason, re.M)
        out: list[str] = []
        cursor = 1
        for _hdr, oa, ob, ins, _na, _nb, body in blocks:
            if ins:
                oa_i, ob_i = int(ins) + 1, int(ins)
            else:
                oa_i, ob_i = int(oa), int(ob)
            out.extend(old_lines[cursor - 1:oa_i - 1])
            out.extend(re.sub(r"^\d+: ", "", ln) for ln in body.rstrip("\n").split("\n"))
            cursor = ob_i + 1
        out.extend(old_lines[cursor - 1:])
        return "\n".join(out) + "\n"

    def test_large_change_inside_one_function_returns_that_function(self) -> None:
        funcs = [f"def fn_{i}(a, b):\n" + "".join(f"    v{j} = a + b + {j}\n" for j in range(12)) + "    return a\n\n\n" for i in range(25)]
        old = "".join(funcs)
        # Too large for the 60-line unified diff, but confined to one function.
        new_body = "def fn_7(a, b):\n" + "".join(f"    w{j} = a * b * {j}\n" for j in range(70)) + "    return b\n\n\n"
        new = old.replace(funcs[7], new_body)
        f = self.tmp / "big.py"
        f.write_bytes(old.encode("utf-8"))
        cfg = dict(self.cfg, outline_first=False)
        self.assertIsNone(read_funnel.check_read("sd", str(f), cfg=cfg, env=self.env))
        f.write_bytes(new.encode("utf-8"))
        res = read_funnel.check_read("sd", str(f), cfg=cfg, env=self.env)
        assert res is not None
        self.assertIn("changed only inside", res["reason"])
        self.assertIn("fn_7", res["reason"])
        self.assertEqual(self._apply_regions(old, res["reason"]), new)
        self.assertLess(len(res["reason"]), len(new) * 0.4)
        # Retry gets the full file.
        self.assertIsNone(read_funnel.check_read("sd", str(f), cfg=cfg, env=self.env))

    def test_change_outside_any_symbol_falls_back(self) -> None:
        old = "".join(f"CONST_{i} = {i}\n" for i in range(300))
        new = "".join(f"CONST_{i} = {i * 7}\n" for i in range(300))
        self.assertIsNone(read_funnel._symbol_region_delta("c.py", old, new))


class EditMissHintTests(_Base):
    def test_whitespace_mismatch_shows_exact_region(self) -> None:
        f = self.tmp / "e.py"
        f.write_text("def a():\n\tif x:\n\t\treturn 1\n\treturn 2\n", encoding="utf-8")
        res = edit_assist.edit_failure_hint(str(f), "    if x:\n        return 1\n", "String to replace not found in file.")
        assert res is not None
        hint, rng = res
        self.assertIn("tabs", hint)
        self.assertIn("     2\t\tif x:", hint)
        self.assertEqual(rng, (1, 4))

    def test_ambiguous_match_lists_lines(self) -> None:
        f = self.tmp / "m.py"
        f.write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")
        res = edit_assist.edit_failure_hint(str(f), "x = 1", "Found 2 matches of the string to replace, but replace_all is false.")
        assert res is not None
        self.assertIn("L1, L3", res[0])

    def test_post_tool_use_failure_injects_hint(self) -> None:
        f = self.tmp / "h.py"
        f.write_text("def greet(name):\n    return 'hi ' + name\n", encoding="utf-8")
        payload = {
            "session_id": "edit-miss",
            "cwd": str(self.tmp),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(f), "old_string": "def greet(name) :\n    return 'hi ' + name", "new_string": "x"},
            "error": "String to replace not found in file.",
        }
        out = unified_hook.handle_post_tool_use("claude", payload, self.cfg, self.env, is_failure_event=True)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUseFailure")
        self.assertIn("def greet(name):", ctx)


class SearchRepeatTests(_Base):
    def test_identical_result_elided_once_then_served(self) -> None:
        out = "\n".join(f"src/file_{i}.py:10: match" for i in range(60))
        args = {"pattern": "match"}
        self.assertIsNone(read_funnel.check_search_repeat("sr", "Grep", args, out, env=self.env))
        msg = read_funnel.check_search_repeat("sr", "Grep", args, out, env=self.env)
        self.assertIsNotNone(msg)
        self.assertIsNone(read_funnel.check_search_repeat("sr", "Grep", args, out, env=self.env))
        # Changed output is never elided.
        self.assertIsNone(read_funnel.check_search_repeat("sr", "Grep", args, out + "\nnew.py:1: match", env=self.env))
        # /compact clears the state.
        read_funnel.clear_session_cache("sr", env=self.env)
        self.assertIsNone(read_funnel.check_search_repeat("sr", "Grep", args, out + "\nnew.py:1: match", env=self.env))


    def test_glob_result_differing_only_in_duration_is_elided(self) -> None:
        files = [f"src/pkg/file_{i}.py" for i in range(80)]
        cfg = dict(self.cfg)

        def run(duration: int) -> dict:
            payload = {
                "session_id": "glob-rep", "cwd": str(self.tmp), "tool_name": "Glob",
                "tool_input": {"pattern": "**/*.py"},
                "tool_response": {"filenames": files, "durationMs": duration, "numFiles": 80, "truncated": False},
            }
            return unified_hook.handle_post_tool_use("claude", payload, cfg, self.env)

        run(12)
        second = run(31)
        self.assertIn("identical result", second["hookSpecificOutput"]["updatedToolOutput"])
        third = run(18)
        self.assertNotIn("identical result", str(third))


class FunnelMetricsAndStabilityTests(_Base):
    def test_late_reread_is_not_counted_as_retry(self) -> None:
        f = self.tmp / "late.py"
        f.write_text("a = 1\n", encoding="utf-8")
        read_funnel.check_read("lt", str(f), cfg=self.cfg, env=self.env)
        self.assertIsNotNone(read_funnel.check_read("lt", str(f), cfg=self.cfg, env=self.env))
        store = read_funnel._load_store("lt", self.env)
        for entry in store["reads"].values():
            entry["ts"] = time.time() - 3600
        read_funnel._save_store("lt", store, time.time(), self.env)
        self.assertIsNone(read_funnel.check_read("lt", str(f), cfg=self.cfg, env=self.env))
        self.assertEqual(config.load_funnel_summary(self.env)["t0"]["retry"], 0)

    def test_t0_served_and_retry_are_counted(self) -> None:
        f = self.tmp / "m.py"
        f.write_text("a = 1\n", encoding="utf-8")
        read_funnel.check_read("mx", str(f), cfg=self.cfg, env=self.env)
        self.assertIsNotNone(read_funnel.check_read("mx", str(f), cfg=self.cfg, env=self.env))
        self.assertIsNone(read_funnel.check_read("mx", str(f), cfg=self.cfg, env=self.env))
        summary = config.load_funnel_summary(self.env)
        self.assertEqual(summary["t0"]["served"], 1)
        self.assertEqual(summary["t0"]["retry"], 1)

    def test_session_brief_is_byte_stable(self) -> None:
        ws = self.tmp / "ws"
        ws.mkdir()
        (ws / "main.py").write_text("import util\n\ndef main():\n    util.go()\n", encoding="utf-8")
        (ws / "util.py").write_text("def go():\n    return 1\n", encoding="utf-8")
        first = session_brief.build_session_brief(str(ws), host="claude", cfg=self.cfg, env=self.env)
        time.sleep(0.05)
        second = session_brief.build_session_brief(str(ws), host="claude", cfg=self.cfg, env=self.env)
        self.assertEqual(first, second)


class SmartCrusherAndLinterLosslessTests(unittest.TestCase):
    def test_smart_crusher_lossless_prefix_dict_sparse_and_nested(self) -> None:
        import json
        from runtime import smart_crusher

        items = [
            {
                "id": i,
                "status": "completed" if i % 2 else "in_progress",
                "url": f"https://api.github.com/repos/org/project/issues/{i}",
                "user": {"login": f"dev{i % 3}", "type": "User"},
                "error": "timeout" if i == 7 else None,
                "draft": False,
            }
            for i in range(1, 25)
        ]
        raw = json.dumps(items, indent=2)
        crushed = smart_crusher.crush_json(raw, allow_sample=False)
        self.assertIsNotNone(crushed)
        assert crushed is not None
        self.assertLess(len(crushed), int(len(raw) * 0.25))
        doc = json.loads(crushed)
        self.assertEqual(doc["_format"], "columnar")
        self.assertEqual(doc["_const"]["user.type"], "User")
        self.assertEqual(doc["_const"]["draft"], False)
        self.assertEqual(doc["_seq"]["id"], {"start": 1, "step": 1})
        self.assertEqual(doc["_prefix"]["url"], "https://api.github.com/repos/org/project/issues/")
        self.assertIn("status", doc["_dict"])
        self.assertEqual(doc["_default"]["error"], None)
        self.assertEqual(doc["_sparse"]["error"], {"6": "timeout"})

    def test_linter_handler_preserves_every_diagnostic_and_dedups_footers(self) -> None:
        from runtime import command_handlers

        lines = []
        for i in range(1, 8):
            lines.append(f"src/mod_{i}.py:{i * 10}:5: error: Incompatible return value type [return-value]")
            lines.append("  note: See https://mypy.readthedocs.io/en/stable/error_codes.html")
            lines.append("  help: Fix the return annotation")
        lines.append("Found 7 errors in 7 files (checked 20 source files)")
        raw = "\n".join(lines) + "\n"
        out = command_handlers.compress("mypy src/", raw, returncode=1)
        for i in range(1, 8):
            self.assertIn(f"src/mod_{i}.py:{i * 10}:5: error: Incompatible return value type", out)
        self.assertIn("Found 7 errors in 7 files", out)
        self.assertEqual(out.count("See https://mypy.readthedocs.io"), 1)
        self.assertIn("repeated linter note/help", out)


if __name__ == "__main__":
    unittest.main()
