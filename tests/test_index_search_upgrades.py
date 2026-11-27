"""Tests for the code-graph index actions and the upgraded archive search."""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime import context_sandbox, index_mcp  # noqa: E402

os.environ.setdefault("TERSER_PYTHON_WORKER", "0")

_FILES = {
    "pkg/__init__.py": "",
    "pkg/alpha.py": (
        "from pkg import beta\n"
        "\n"
        "\n"
        "def helper():\n"
        "    return beta.compute(1)\n"
        "\n"
        "\n"
        "def unused_thing():\n"
        "    return 0\n"
    ),
    "pkg/beta.py": (
        "from pkg import alpha\n"
        "\n"
        "\n"
        "def compute(x):\n"
        "    return square(x)\n"
        "\n"
        "\n"
        "def square(x):\n"
        "    return x * x\n"
    ),
    "app.py": (
        "from pkg.alpha import helper\n"
        "\n"
        "\n"
        "def run():\n"
        "    return helper()\n"
    ),
}


class _RepoBase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.env = {"TERSER_HOME": str(self.tmp / "state"), "TERSER_INDEX_PATH": str(self.tmp / "index")}
        self.repo_dir = self.tmp / "proj"
        for rel, body in _FILES.items():
            p = self.repo_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        res = index_mcp.index_folder(str(self.repo_dir), env=self.env)
        self.repo = res["repo"]

    def tearDown(self) -> None:
        self._td.cleanup()

    def act(self, action: str, **args):
        return index_mcp.dispatch_action(action, {"repo": self.repo, **args}, env=self.env)

    def text(self, action: str, **args) -> str:
        return index_mcp.format_mcp_response(action, self.act(action, **args))


class ReferenceAndImporterTests(_RepoBase):
    def test_find_references_excludes_definition_line(self) -> None:
        res = self.act("find_references", symbol="helper")
        by_file = {r["file"]: r["lines"] for r in res["references"]}
        self.assertEqual(by_file, {"app.py": [1, 5]})
        self.assertEqual(res["definitions"], ["pkg/alpha.py::helper"])
        out = self.text("find_references", symbol="helper")
        self.assertIn("app.py: 1,5", out)
        self.assertFalse(out.lstrip().startswith("{"))

    def test_find_importers_by_dotted_name_and_path(self) -> None:
        res = self.act("find_importers", module="pkg.alpha")
        self.assertEqual(res["targets"], ["pkg/alpha.py"])
        self.assertEqual({i["file"] for i in res["importers"]}, {"app.py", "pkg/beta.py"})
        res2 = self.act("find_importers", module="pkg/alpha.py")
        self.assertEqual(res2["importers"], res["importers"])

    def test_missing_args_and_unknown_repo_error(self) -> None:
        self.assertIn("error", self.act("find_references"))
        self.assertIn("error", self.act("find_importers"))
        res = index_mcp.dispatch_action("find_references", {"repo": "nope-x", "symbol": "a"}, env=self.env)
        self.assertIn("error", res)


class CallHierarchyTests(_RepoBase):
    def test_callers_and_callees(self) -> None:
        res = self.act("get_call_hierarchy", symbol="compute", depth=2)
        self.assertEqual(res["symbol"], "pkg/beta.py::compute")
        callers = res["callers"]
        self.assertEqual([c["id"] for c in callers], ["pkg/alpha.py::helper"])
        self.assertEqual([c["id"] for c in callers[0]["children"]], ["app.py::run"])
        self.assertEqual([c["id"] for c in res["callees"]], ["pkg/beta.py::square"])
        out = self.text("get_call_hierarchy", symbol="compute")
        self.assertIn("    app.py::run L4", out)

    def test_depth_one_stops_early(self) -> None:
        res = self.act("get_call_hierarchy", symbol="compute", depth=1)
        self.assertEqual(res["callers"][0]["children"], [])

    def test_unknown_symbol(self) -> None:
        self.assertIn("error", self.act("get_call_hierarchy", symbol="does_not_exist"))


class SafetyVerdictTests(_RepoBase):
    def test_delete_verdicts(self) -> None:
        self.assertEqual(self.act("check_delete_safe", symbol="unused_thing")["verdict"], "SAFE")
        res = self.act("check_delete_safe", symbol="helper")
        self.assertEqual(res["verdict"], "UNSAFE")
        self.assertIn("app.py", {a["file"] for a in res["affected_files"]})
        self.assertTrue(self.text("check_delete_safe", symbol="helper").startswith("UNSAFE: delete helper"))

    def test_edit_verdicts(self) -> None:
        self.assertEqual(self.act("check_edit_safe", symbol="square")["verdict"], "SAFE")
        self.assertEqual(self.act("check_edit_safe", symbol="helper")["verdict"], "CAUTION")
        self.assertEqual(self.act("check_edit_safe", symbol="nothing_here")["verdict"], "UNKNOWN")

    def test_rename_verdicts(self) -> None:
        self.assertEqual(self.act("check_rename_safe", symbol="compute", new_name="square")["verdict"], "CONFLICT")
        res = self.act("check_rename_safe", symbol="compute", new_name="calculate")
        self.assertEqual(res["verdict"], "REVIEW")
        self.assertEqual({a["file"] for a in res["affected_files"]}, {"pkg/alpha.py", "pkg/beta.py"})
        self.assertEqual(self.act("check_rename_safe", symbol="square", new_name="sq")["verdict"], "SAFE")


class DeadCodeAndCycleTests(_RepoBase):
    def test_dead_code(self) -> None:
        ids = {c["id"] for c in self.act("find_dead_code")["candidates"]}
        self.assertIn("pkg/alpha.py::unused_thing", ids)
        self.assertIn("app.py::run", ids)
        self.assertNotIn("pkg/beta.py::square", ids)

    def test_decorated_symbols_are_not_dead(self) -> None:
        p = self.repo_dir / "routes.py"
        p.write_text("import app\n\n\n@app.route('/')\ndef index_page():\n    return 1\n", encoding="utf-8")
        index_mcp.index_folder(str(self.repo_dir), env=self.env)
        ids = {c["id"] for c in self.act("find_dead_code")["candidates"]}
        self.assertNotIn("routes.py::index_page", ids)

    def test_dependency_cycles(self) -> None:
        res = self.act("get_dependency_cycles")
        self.assertEqual(len(res["cycles"]), 1)
        cyc = res["cycles"][0]
        self.assertEqual(set(cyc["members"]), {"pkg/alpha.py", "pkg/beta.py"})
        self.assertEqual(cyc["cycle"][0], cyc["cycle"][-1])
        self.assertIn("pkg/alpha.py -> pkg/beta.py -> pkg/alpha.py", self.text("get_dependency_cycles"))

    def test_no_cycles(self) -> None:
        (self.repo_dir / "pkg/beta.py").write_text("def compute(x):\n    return x\n", encoding="utf-8")
        index_mcp.index_folder(str(self.repo_dir), env=self.env)
        self.assertEqual(self.act("get_dependency_cycles")["cycles"], [])
        self.assertIn("no import cycles", self.text("get_dependency_cycles"))

    def test_strongly_connected_is_iterative(self) -> None:
        chain = {f"f{i}": {f"f{i + 1}"} for i in range(3000)}
        chain["f3000"] = {"f0"}
        comps = index_mcp._strongly_connected(chain)
        self.assertEqual(len(comps), 1)
        self.assertEqual(len(comps[0]), 3001)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class ChangedSymbolsTests(_RepoBase):
    def _git(self, *argv: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *argv],
            cwd=self.repo_dir, check=True, capture_output=True,
        )

    def test_diff_hunks_map_to_symbols(self) -> None:
        self._git("init", "-q")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "init")
        beta = self.repo_dir / "pkg/beta.py"
        beta.write_text(beta.read_text(encoding="utf-8").replace("x * x", "x ** 2"), encoding="utf-8")
        (self.repo_dir / "extra.py").write_text("def brand_new():\n    return 1\n", encoding="utf-8")
        res = self.act("get_changed_symbols", ref="HEAD")
        files = {f["file"]: f for f in res["files"]}
        self.assertEqual([s["id"] for s in files["pkg/beta.py"]["symbols"]], ["pkg/beta.py::square"])
        self.assertEqual(files["extra.py"]["status"], "added")
        self.assertEqual([s["id"] for s in files["extra.py"]["symbols"]], ["extra.py::brand_new"])
        out = self.text("get_changed_symbols", ref="HEAD")
        self.assertIn("pkg/beta.py::square L8-9", out)
        self.assertIn("extra.py [added]:", out)

    def test_bad_ref_rejected(self) -> None:
        self.assertIn("error", self.act("get_changed_symbols", ref="--output=x"))


class OrderAndRouteTests(_RepoBase):
    def test_order_reaches_new_actions(self) -> None:
        with mock.patch.dict(os.environ, self.env):
            for action, args in (
                ("find_references", {"symbol": "helper"}),
                ("find_importers", {"module": "pkg.beta"}),
                ("get_call_hierarchy", {"symbol": "helper", "depth": 1}),
                ("check_edit_safe", {"symbol": "helper"}),
                ("check_delete_safe", {"symbol": "helper"}),
                ("check_rename_safe", {"symbol": "helper", "new_name": "aid"}),
                ("find_dead_code", {}),
                ("get_dependency_cycles", {}),
            ):
                name, payload = index_mcp._handle_tool_call("order", {"action": action, "args": {"repo": self.repo, **args}})
                self.assertEqual(name, action)
                self.assertNotIn("error", payload, action)
            # Numeric strings are a common model slip and are coerced; non-numbers still fail.
            name, _ = index_mcp._handle_tool_call("order", {"action": "get_call_hierarchy", "args": {"symbol": "x", "depth": "2"}})
            self.assertEqual(name, "get_call_hierarchy")
            with self.assertRaises(ValueError):
                index_mcp._handle_tool_call("order", {"action": "get_call_hierarchy", "args": {"symbol": "x", "depth": "two"}})

    def test_menu_and_route(self) -> None:
        _, menu = index_mcp._handle_tool_call("menu", {})
        for action in sorted(index_mcp._GRAPH_ACTIONS | {"purge_sandbox"}):
            self.assertIn(action, menu["actions"])
        cases = {
            "who imports the config module": "find_importers",
            "is it safe to rename this": "check_rename_safe",
            "find dead code": "find_dead_code",
            "show circular imports": "get_dependency_cycles",
            "purge old archives": "purge_sandbox",
            "callers of foo": "get_call_hierarchy",
        }
        for task, expected in cases.items():
            _, res = index_mcp._handle_tool_call("route", {"task": task})
            self.assertEqual(res["recommended_action"], expected, task)

    def test_skill_doc_lists_only_existing_actions(self) -> None:
        doc = (ROOT / "skills" / "terser-index" / "SKILL.md").read_text(encoding="utf-8")
        tools = {t["name"] for t in index_mcp._MCP_TOOLS}
        prefixes = ("get_", "find_", "check_", "search_", "expand_", "index_", "list_", "invalidate_", "assemble_", "execute_", "purge_")
        named = {m for m in re.findall(r"`([a-z_]+)`", doc) if m.startswith(prefixes)}
        named.discard("index_auto_init_max_files")
        unknown = named - set(index_mcp._ACTIONS_MENU) - tools
        self.assertEqual(unknown, set())
        for action in index_mcp._ACTIONS_MENU:
            self.assertIn(f"`{action}`", doc)


class ArchiveSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.env = {"TERSER_HOME": self._td.name}

    def tearDown(self) -> None:
        self._td.cleanup()

    def _store(self, body: str, session: str = "") -> str:
        aid = context_sandbox.store_output(body, command="test", env=self.env, session_id=session)
        self.assertTrue(aid)
        return aid

    def _log(self, needle_line: str) -> str:
        filler = [f"info: step {i} completed normally" for i in range(120)]
        filler[70] = needle_line
        return "\n".join(filler)

    def test_rrf_merge_rewards_agreement(self) -> None:
        a, b, c = (1, 10, "a"), (11, 20, "b"), (21, 30, "c")
        merged = context_sandbox.rrf_merge([[a, b], [b, c]], limit=3)
        self.assertEqual(merged[0], b)
        self.assertEqual(set(merged), {a, b, c})
        self.assertEqual(context_sandbox.rrf_merge([[a, b], [b, c]], limit=1), [b])

    def test_trigram_finds_partial_words(self) -> None:
        aid = self._store(self._log("error: PaymentGatewayTimeoutError raised by client"))
        res = context_sandbox.query_output(aid, "Gateway Timeout", env=self.env) or ""
        self.assertIn("PaymentGatewayTimeoutError", res)
        self.assertNotIn("spelling corrected", res)

    def test_porter_stemming_still_works(self) -> None:
        aid = self._store(self._log("warning: the workers were restarting repeatedly"))
        res = context_sandbox.query_output(aid, "restarted worker", env=self.env) or ""
        self.assertIn("restarting repeatedly", res)

    def test_spelling_correction_on_miss(self) -> None:
        aid = self._store(self._log("fatal: connection refused by upstream"))
        res = context_sandbox.query_output(aid, "conection", env=self.env) or ""
        self.assertIn("spelling corrected", res)
        self.assertIn("`connection`", res)
        self.assertIn("connection refused by upstream", res)
        self.assertIsNone(context_sandbox.query_output(aid, "conection", env=self.env, require_hit=True))
        miss = context_sandbox.query_output(aid, "zzqqxxyy", env=self.env) or ""
        self.assertIn("no matches", miss)

    def test_suggest_correction_keeps_known_words(self) -> None:
        vocab = {"connection", "refused", "upstream"}
        self.assertIsNone(context_sandbox.suggest_correction("refused", vocab))
        self.assertEqual(context_sandbox.suggest_correction("refused conection", vocab), "refused connection")

    def test_trigram_backfill_for_existing_db(self) -> None:
        aid = self._store(self._log("error: PaymentGatewayTimeoutError raised"))
        db = context_sandbox._db_path(self.env)
        with closing(sqlite3.connect(str(db))) as conn:
            conn.execute("DROP TABLE chunks_tri")
            conn.commit()
        res = context_sandbox.query_output(aid, "GatewayTimeout", env=self.env) or ""
        self.assertIn("PaymentGatewayTimeoutError", res)
        with closing(sqlite3.connect(str(db))) as conn:
            n = conn.execute("SELECT COUNT(*) FROM chunks_tri WHERE blob_id = ?", (aid,)).fetchone()[0]
        self.assertGreater(n, 0)

    def test_purge_by_id_session_and_all(self) -> None:
        a1 = self._store("alpha output one\n" * 5, session="s1")
        a2 = self._store("beta output two\n" * 5, session="s1")
        a3 = self._store("gamma output three\n" * 5, session="s2")
        a4 = self._store("delta output four\n" * 5, session="s2")
        self.assertIn("error", context_sandbox.purge_archives(env=self.env))

        res = index_mcp.dispatch_action("purge_sandbox", {"session": "s1"}, env=self.env)
        self.assertEqual(res["deleted"], 2)
        self.assertEqual(set(res["ids"]), {a1, a2})
        self.assertIsNone(context_sandbox.expand_output(a1, env=self.env))
        self.assertIsNotNone(context_sandbox.expand_output(a3, env=self.env))
        self.assertIn("Purged 2 archive(s)", index_mcp.format_mcp_response("purge_sandbox", res))

        res = index_mcp.dispatch_action("purge_sandbox", {"archive_id": a3}, env=self.env)
        self.assertEqual(res["deleted"], 1)
        db = context_sandbox._db_path(self.env)
        with closing(sqlite3.connect(str(db))) as conn:
            for table in ("chunks_fts", "chunks_tri"):
                n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE blob_id IN (?, ?, ?)", (a1, a2, a3)).fetchone()[0]
                self.assertEqual(n, 0, table)

        raw_dir = context_sandbox._raw_archives_dir(self.env)
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / "abcdef123456.txt").write_text("raw sidecar\n", encoding="utf-8")
        res = index_mcp.dispatch_action("purge_sandbox", {"all": True}, env=self.env)
        self.assertEqual(res["deleted"], 1)
        self.assertEqual(res["raw_deleted"], 1)
        self.assertIsNone(context_sandbox.expand_output(a4, env=self.env))

    def test_session_defaults_from_env(self) -> None:
        env = dict(self.env, TERSER_SESSION_ID="sess-env")
        aid = context_sandbox.store_output("from env session\n" * 3, command="x", env=env)
        res = context_sandbox.purge_archives(session_id="sess-env", env=self.env)
        self.assertEqual(res["ids"], [aid])


if __name__ == "__main__":
    unittest.main()
