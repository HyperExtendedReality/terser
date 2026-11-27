"""Tests for the MCP proxy, batch execute, post-compaction recall, and status line."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime import context_sandbox, index_mcp, mcp_proxy  # noqa: E402

_FAKE_SERVER = r'''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("method") == "tools/list":
        res = {"tools": [{"name": "search", "description": "Search things.\n\nLong details " + "x" * 900,
               "inputSchema": {"type": "object", "title": "T", "properties": {"q": {"type": "string",
               "description": "query " * 80, "examples": ["a"]}}, "required": ["q"]}}]}
    elif msg.get("method") == "tools/call":
        rows = [{"id": i, "name": f"item-{i}", "zone": "eu"} for i in range(80)]
        res = {"content": [{"type": "text", "text": json.dumps(rows, indent=2)}]}
    else:
        res = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}) + "\n")
    sys.stdout.flush()
'''


class McpProxyTests(unittest.TestCase):
    def test_proxy_slims_list_and_compresses_call(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            srv = Path(td) / "srv.py"
            srv.write_text(_FAKE_SERVER, encoding="utf-8")
            reqs = [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search", "arguments": {"q": "a"}}},
            ]
            env = dict(os.environ, TERSER_HOME=str(Path(td) / "home"), PYTHONUTF8="1")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "runtime" / "mcp_proxy.py"), "--", sys.executable, str(srv)],
                input="".join(json.dumps(r) + "\n" for r in reqs), capture_output=True, text=True, env=env, timeout=60,
            )
            out = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
            self.assertEqual(len(out), 2)
            tool = out[0]["result"]["tools"][0]
            self.assertEqual(tool["description"], "Search things.")
            self.assertNotIn("title", tool["inputSchema"])
            self.assertNotIn("examples", tool["inputSchema"]["properties"]["q"])
            self.assertEqual(tool["inputSchema"]["required"], ["q"])
            self.assertLessEqual(len(tool["inputSchema"]["properties"]["q"]["description"]), 163)
            text = out[1]["result"]["content"][0]["text"]
            self.assertLess(len(text), len(json.dumps([{"id": i, "name": f"item-{i}", "zone": "eu"} for i in range(80)], indent=2)))

    def test_error_results_untouched(self) -> None:
        res = {"isError": True, "content": [{"type": "text", "text": "boom " * 1000}]}
        self.assertIs(mcp_proxy.compress_call_result(res, "x"), res)


class BatchTests(unittest.TestCase):
    def test_batch_runs_each_command(self) -> None:
        r = index_mcp._exec_batch({"commands": [[sys.executable, "-c", "print('alpha')"], [sys.executable, "-c", "print('beta')"]]})
        self.assertIn("alpha", r["batch"])
        self.assertIn("beta", r["batch"])
        self.assertIn("error", index_mcp._exec_batch({"commands": []}))


class RecallTests(unittest.TestCase):
    def test_recall_matches_prompt_terms(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ, TERSER_HOME=td)
            ws = str(Path(td) / "proj")
            context_sandbox.record_session_event("s1", ws, "edit", "runtime/parser.py: tokenizer rewrite", env=env)
            context_sandbox.record_session_event("s1", ws, "error", "pytest: test_tokenizer failed on unicode", env=env)
            context_sandbox.record_session_event("s1", ws, "cmd", "npm run build", env=env)
            lines = context_sandbox.recall_events(ws, "why is the tokenizer test failing", session_id="s1", env=env)
            self.assertTrue(lines)
            self.assertIn("error", lines[0])
            self.assertFalse(any("npm run build" in l for l in lines))


class StatusLineTests(unittest.TestCase):
    def test_statusline_prints_one_line(self) -> None:
        from runtime import statusline

        line = statusline.render({"session_id": "nope", "transcript_path": ""}, env=dict(os.environ))
        self.assertIn("Terser", line)
        self.assertNotIn("\n", line)


if __name__ == "__main__":
    unittest.main()
