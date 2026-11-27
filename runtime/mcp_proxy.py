#!/usr/bin/env python3
"""Terser MCP proxy: wrap any stdio MCP server and shrink what it sends back.

Usage (in an MCP config, prefix the server's command):
    python runtime/mcp_proxy.py -- npx -y @modelcontextprotocol/server-github

What it changes, and only this:

* ``tools/list`` responses: each tool description is cut to its first paragraph (max
  300 chars); parameter descriptions to 160 chars; ``examples``/``$schema``/``title``
  keys are dropped from input schemas. Names, types, ``required`` and enums are kept,
  so every call the agent could make before is still valid.
* ``tools/call`` results: text items over 1.5 KB go through Terser's output pipeline.
  Items up to 8 KB get lossless transforms only; larger ones may be windowed, with the
  full text archived locally and a retrieval hint attached. Error results
  (``isError: true``) and non-text items are never touched.

Everything else is forwarded byte-for-byte. Stdlib only; on any parse or compression
error the original line is forwarded unchanged (fail-open).

Flags (before ``--``):

* ``--lazy-tools``: cut every tool description to its first sentence (max 120 chars) and
  drop parameter descriptions, then add one ``terser_tool_help`` tool that returns any
  tool's original description and full input schema on demand. The proxy answers that
  call itself; the wrapped server never sees it. Servers that spend 10-40k tokens on
  ``tools/list`` shrink to names, types, and one line each.
* ``--no-desc``: leave ``tools/list`` untouched (results are still compressed).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
if str(_THIS.parents[1]) not in sys.path:
    sys.path.insert(0, str(_THIS.parents[1]))

_DESC_MAX = 300
_PARAM_DESC_MAX = 160
_COMPRESS_MIN = 1500
_LOSSLESS_MAX = 8000
_DROP_SCHEMA_KEYS = ("examples", "$schema", "title", "$comment")


def _cut(text: str, limit: int) -> str:
    text = str(text or "").strip()
    para = text.split("\n\n", 1)[0].strip()
    if len(para) <= limit:
        return para
    cut = para[:limit]
    dot = cut.rfind(". ")
    return (cut[: dot + 1] if dot > limit // 2 else cut.rstrip() + "...")


def _slim_schema(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k in _DROP_SCHEMA_KEYS:
                continue
            if k == "description" and isinstance(v, str):
                out[k] = _cut(v, _PARAM_DESC_MAX)
            else:
                out[k] = _slim_schema(v)
        return out
    if isinstance(node, list):
        return [_slim_schema(x) for x in node]
    return node


def slim_tools_list(result: dict[str, Any]) -> dict[str, Any]:
    tools = result.get("tools")
    if not isinstance(tools, list):
        return result
    slim = []
    for tool in tools:
        if not isinstance(tool, dict):
            slim.append(tool)
            continue
        t = dict(tool)
        if isinstance(t.get("description"), str):
            t["description"] = _cut(t["description"], _DESC_MAX)
        if isinstance(t.get("inputSchema"), dict):
            t["inputSchema"] = _slim_schema(t["inputSchema"])
        slim.append(t)
    return dict(result, tools=slim)


HELP_TOOL = "terser_tool_help"
_LAZY_DESC_MAX = 120


def _first_sentence(text: str, limit: int = _LAZY_DESC_MAX) -> str:
    para = _cut(text, 10_000)
    m = re.search(r"[.!?](?:\s|$)", para)
    sentence = para[: m.end()].strip() if m else para
    if len(sentence) <= limit:
        return sentence
    return sentence[:limit].rstrip() + "..."


def _strip_param_descriptions(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: _strip_param_descriptions(v)
            for k, v in node.items()
            if k not in _DROP_SCHEMA_KEYS and not (k == "description" and isinstance(v, str))
        }
    if isinstance(node, list):
        return [_strip_param_descriptions(x) for x in node]
    return node


def lazy_tools_list(result: dict[str, Any], originals: dict[str, dict[str, Any]], *, add_help: bool = True) -> dict[str, Any]:
    """One-line descriptions + schema without descriptions; originals kept for `terser_tool_help`."""
    tools = result.get("tools")
    if not isinstance(tools, list):
        return result
    slim = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            slim.append(tool)
            continue
        originals[tool["name"]] = tool
        t = dict(tool)
        if isinstance(t.get("description"), str):
            t["description"] = _first_sentence(t["description"])
        if isinstance(t.get("inputSchema"), dict):
            t["inputSchema"] = _strip_param_descriptions(t["inputSchema"])
        slim.append(t)
    if add_help and not any(isinstance(t, dict) and t.get("name") == HELP_TOOL for t in slim):
        slim.append({
            "name": HELP_TOOL,
            "description": "Full description and parameter docs for any tool of this server (descriptions are shortened).",
            "inputSchema": {
                "type": "object",
                "properties": {"tool": {"type": "string"}},
                "required": ["tool"],
            },
        })
    return dict(result, tools=slim)


def tool_help_result(originals: dict[str, dict[str, Any]], name: str) -> dict[str, Any]:
    tool = originals.get(name)
    if tool is None:
        known = ", ".join(sorted(originals)) or "(tools/list not seen yet)"
        return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool `{name}`. Known: {known}"}]}
    body = [f"# {name}", str(tool.get("description") or "").strip()]
    schema = tool.get("inputSchema")
    if isinstance(schema, dict):
        body.append("Input schema:\n" + json.dumps(schema, ensure_ascii=False, indent=1))
    return {"content": [{"type": "text", "text": "\n\n".join(x for x in body if x)}]}


def compress_call_result(result: dict[str, Any], label: str) -> dict[str, Any]:
    if result.get("isError") or not isinstance(result.get("content"), list):
        return result
    from runtime import output_compressor

    content = []
    changed = False
    for item in result["content"]:
        text = item.get("text") if isinstance(item, dict) and item.get("type") == "text" else None
        if not isinstance(text, str) or len(text) < _COMPRESS_MIN:
            content.append(item)
            continue
        try:
            served, _ = output_compressor.compress_output(
                text, command=label, lossless_only=len(text) <= _LOSSLESS_MAX, host="mcp-proxy"
            )
        except Exception:
            served = text
        if served and len(served) < len(text):
            item = dict(item, text=served)
            changed = True
        content.append(item)
    return dict(result, content=content) if changed else result


class _Proxy:
    def __init__(self, argv: list[str], *, lazy_tools: bool = False, keep_desc: bool = False) -> None:
        self.argv = argv
        self.label = Path(argv[0]).name
        self.lazy_tools = lazy_tools
        self.keep_desc = keep_desc
        self.originals: dict[str, dict[str, Any]] = {}
        self.pending: dict[Any, tuple[str, str, bool]] = {}  # request id -> (method, tool name, first page)
        self.lock = threading.Lock()
        self.out_lock = threading.Lock()

    def _emit(self, data: bytes) -> None:
        with self.out_lock:
            out = sys.stdout.buffer
            out.write(data)
            out.flush()

    def _answer_locally(self, msg: Any) -> bool:
        """Answer `terser_tool_help` here; return True when the request was consumed."""
        if not (self.lazy_tools and isinstance(msg, dict) and msg.get("method") == "tools/call" and "id" in msg):
            return False
        params = msg.get("params") or {}
        if not isinstance(params, dict) or params.get("name") != HELP_TOOL:
            return False
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        result = tool_help_result(self.originals, str(args.get("tool") or ""))
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        self._emit((json.dumps(reply, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        return True

    def _client_to_server(self, child: subprocess.Popen) -> None:
        assert child.stdin is not None
        for raw in sys.stdin.buffer:
            try:
                msg = json.loads(raw)
                if self._answer_locally(msg):
                    continue
                if isinstance(msg, dict) and "id" in msg and msg.get("method") in ("tools/list", "tools/call"):
                    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
                    tool = str(params.get("name") or "")
                    first_page = not params.get("cursor")
                    with self.lock:
                        self.pending[msg["id"]] = (msg["method"], tool, first_page)
            except (ValueError, TypeError):
                pass
            try:
                child.stdin.write(raw)
                child.stdin.flush()
            except OSError:
                break
        try:
            child.stdin.close()
        except OSError:
            pass

    def _rewrite(self, raw: bytes) -> bytes:
        try:
            msg = json.loads(raw)
        except ValueError:
            return raw
        if not isinstance(msg, dict) or "id" not in msg or not isinstance(msg.get("result"), dict):
            return raw
        with self.lock:
            method, tool, first_page = self.pending.pop(msg["id"], ("", "", True))
        if method == "tools/list":
            if self.keep_desc:
                return raw
            if self.lazy_tools:
                new = lazy_tools_list(msg["result"], self.originals, add_help=first_page)
            else:
                new = slim_tools_list(msg["result"])
        elif method == "tools/call":
            new = compress_call_result(msg["result"], f"mcp:{self.label}:{tool}")
        else:
            return raw
        if new is msg["result"]:
            return raw
        return (json.dumps(dict(msg, result=new), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    def run(self) -> int:
        child = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None)
        threading.Thread(target=self._client_to_server, args=(child,), daemon=True).start()
        assert child.stdout is not None
        for raw in child.stdout:
            try:
                line = self._rewrite(raw)
            except Exception:
                line = raw
            self._emit(line)
        return child.wait()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    flags: list[str] = []
    if "--" in args:
        flags = args[: args.index("--")]
        args = args[args.index("--") + 1 :]
    if not args:
        print("Usage: mcp_proxy.py [--lazy-tools | --no-desc] -- <server command> [args...]", file=sys.stderr)
        return 2
    if os.environ.get("TERSER_DISABLE") == "1":
        return subprocess.call(args)
    return _Proxy(args, lazy_tools="--lazy-tools" in flags, keep_desc="--no-desc" in flags).run()


if __name__ == "__main__":
    sys.exit(main())
