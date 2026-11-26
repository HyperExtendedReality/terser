#!/usr/bin/env python3
"""Terser — Self-Contained Stdlib AST & Symbol Code-Index MCP Server (`terser-index`).

Zero external dependencies (pure Python stdlib). Implements:
  - JSON-RPC 2.0 MCP over stdio (6-tool Counter front door: `menu`, `order`, `route`,
    `index_folder`, `search_symbols`, `get_symbol_source`)
  - Full AST parsing for Python (`ast`) + structural regex indexing for 25+ languages
    (JS/TS/Rust/Go/Java/Kotlin/C#/C++/Ruby/PHP/Swift/Lua/Zig/GDScript)
  - Persistent local index under `~/.terser/code-index/<repo>.json`
  - Incremental single-file reindex CLI: `python runtime/index_mcp.py index-file <path>`
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _THIS_DIR.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from runtime.config import atomic_write_json, normalize_workspace, state_home

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "terser-index"
SERVER_VERSION = "1.2.0"

_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    "__pycache__", "dist", "build", "target", ".next", ".nuxt",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "vendor",
})

_CODE_EXTS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
    ".rb", ".php", ".swift", ".scala", ".lua", ".dart", ".zig", ".gd", ".sql",
})

_MAX_FILE_BYTES = 512 * 1024

_GENERIC_SYM_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?:export\s+|default\s+|public\s+|private\s+|protected\s+|static\s+|async\s+|pub(?:\([^)]*\))?\s+|override\s+|virtual\s+|unsafe\s+)*"
    r"(?P<kind>class|interface|struct|enum|trait|impl|function|fn|func|def|type|module)\s+"
    r"(?P<name>[A-Za-z_][\w$.<>]*)"
)

_ACTIONS_MENU: dict[str, dict[str, str]] = {
    "index_folder": {"args": '{"path": "<abs-path>"}', "desc": "Index or refresh a local repository folder"},
    "list_repos": {"args": "{}", "desc": "List all indexed repositories"},
    "get_file_tree": {"args": '{"repo": "<name>"}', "desc": "List indexed source files in a repo"},
    "get_file_outline": {"args": '{"repo": "<name>", "file_path": "<rel-path>"}', "desc": "List symbols & line spans in a file"},
    "get_repo_outline": {"args": '{"repo": "<name>"}', "desc": "Compact top-level file/symbol overview"},
    "search_symbols": {"args": '{"repo": "<name>", "query": "<terms>"}', "desc": "Search functions, classes, structs, methods"},
    "get_symbol_source": {"args": '{"repo": "<name>", "symbol_id": "<id>"}', "desc": "Fetch exact source code for a symbol"},
    "get_symbols": {"args": '{"repo": "<name>", "symbol_ids": ["<id>"]}', "desc": "Fetch exact source for multiple symbols"},
    "search_text": {"args": '{"repo": "<name>", "query": "<text>"}', "desc": "Fast text search across indexed files"},
    "get_blast_radius": {"args": '{"repo": "<name>", "symbol": "<name>"}', "desc": "Find files referencing a symbol"},
    "assemble_task_context": {"args": '{"repo": "<name>", "task": "<desc>", "token_budget": 4000}', "desc": "Assemble top matching symbols within a token budget"},
    "invalidate_cache": {"args": '{"repo": "<name>"}', "desc": "Delete stored index for a repository"},
}


def index_dir(env: dict[str, str] | None = None) -> Path:
    e = env if env is not None else os.environ
    raw = (e.get("TERSER_INDEX_PATH") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    return state_home(e) / "code-index"


def _repo_slug(root: Path) -> str:
    norm = normalize_workspace(str(root))
    base = re.sub(r"[^a-zA-Z0-9_-]+", "-", root.name).strip("-") or "repo"
    short_hash = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{short_hash}"


def _extract_python_symbols(rel_path: str, text: str) -> list[dict[str, Any]]:
    syms: list[dict[str, Any]] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _extract_generic_symbols(rel_path, text)

    lines = text.splitlines()

    def _visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "class" if isinstance(child, ast.ClassDef) else ("method" if prefix else "function")
                qual = f"{prefix}.{child.name}" if prefix else child.name
                start = getattr(child, "lineno", 1)
                end = getattr(child, "end_lineno", start) or start
                sig = lines[start - 1].strip() if 0 <= start - 1 < len(lines) else child.name
                doc = (ast.get_docstring(child) or "").splitlines()[0][:120] if ast.get_docstring(child) else ""
                syms.append({
                    "id": f"{rel_path}::{qual}",
                    "name": child.name,
                    "qualified_name": qual,
                    "kind": kind,
                    "file": rel_path,
                    "start_line": start,
                    "end_line": end,
                    "signature": sig[:160],
                    "doc": doc,
                })
                if isinstance(child, ast.ClassDef):
                    _visit(child, qual)

    _visit(tree)
    return syms


def _extract_generic_symbols(rel_path: str, text: str) -> list[dict[str, Any]]:
    syms: list[dict[str, Any]] = []
    lines = text.splitlines()
    for idx, line in enumerate(lines, start=1):
        m = _GENERIC_SYM_RE.match(line)
        if not m:
            continue
        kind = m.group("kind")
        name = m.group("name")
        syms.append({
            "id": f"{rel_path}::{name}@{idx}",
            "name": name,
            "qualified_name": name,
            "kind": kind,
            "file": rel_path,
            "start_line": idx,
            "end_line": min(len(lines), idx + 25),
            "signature": line.strip()[:160],
            "doc": "",
        })
    return syms


def _index_single_file(root: Path, file_path: Path) -> list[dict[str, Any]]:
    try:
        st = file_path.stat()
        if not file_path.is_file() or st.st_size > _MAX_FILE_BYTES:
            return []
        raw = file_path.read_bytes()
        if b"\x00" in raw[:2048]:
            return []
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return []
    rel = file_path.relative_to(root).as_posix()
    if file_path.suffix.lower() == ".py":
        return _extract_python_symbols(rel, text)
    return _extract_generic_symbols(rel, text)


def index_folder(path: str, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    root = Path(os.path.expanduser(path)).resolve()
    if not root.is_dir():
        return {"error": f"Directory not found: {path}"}

    slug = _repo_slug(root)
    files: list[str] = []
    symbols: list[dict[str, Any]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for fname in sorted(filenames):
            fp = Path(dirpath) / fname
            if fp.suffix.lower() not in _CODE_EXTS:
                continue
            rel = fp.relative_to(root).as_posix()
            files.append(rel)
            symbols.extend(_index_single_file(root, fp))

    doc = {
        "repo": slug,
        "source_root": str(root),
        "indexed_at": round(time.time(), 1),
        "file_count": len(files),
        "symbol_count": len(symbols),
        "source_files": files,
        "symbols": symbols,
    }
    atomic_write_json(index_dir(env) / f"{slug}.json", doc)
    return {
        "repo": slug,
        "source_root": str(root),
        "files": len(files),
        "symbols": len(symbols),
    }


def _load_repo(repo: str, *, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    idir = index_dir(env)
    if not idir.is_dir():
        return None
    direct = idir / f"{repo}.json"
    if direct.is_file():
        try:
            return json.loads(direct.read_text(encoding="utf-8"))
        except Exception:
            return None
    low = repo.lower()
    for p in idir.glob("*.json"):
        if p.stem.lower() == low or p.stem.lower().startswith(low + "-"):
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
    return None


def reindex_file(file_path: str, *, env: dict[str, str] | None = None) -> bool:
    fp = Path(file_path).resolve()
    if fp.suffix.lower() not in _CODE_EXTS:
        return False
    norm_fp = normalize_workspace(str(fp))
    idir = index_dir(env)
    if not idir.is_dir():
        return False
    for meta_file in idir.glob("*.json"):
        try:
            data = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        sr = str(data.get("source_root") or "")
        norm_sr = normalize_workspace(sr)
        if not norm_sr or not norm_fp.startswith(norm_sr + "/"):
            continue
        root = Path(sr)
        try:
            rel = fp.relative_to(root).as_posix()
        except ValueError:
            continue
        syms = [s for s in (data.get("symbols") or []) if s.get("file") != rel]
        files = set(data.get("source_files") or [])
        if fp.is_file():
            files.add(rel)
            syms.extend(_index_single_file(root, fp))
        else:
            files.discard(rel)
        data["source_files"] = sorted(files)
        data["symbols"] = syms
        data["file_count"] = len(files)
        data["symbol_count"] = len(syms)
        data["indexed_at"] = round(time.time(), 1)
        atomic_write_json(meta_file, data)
        return True
    return False


def _read_symbol_code(repo_doc: dict[str, Any], sym: dict[str, Any]) -> dict[str, Any]:
    root = Path(str(repo_doc.get("source_root") or ""))
    fp = root / str(sym.get("file") or "")
    try:
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"error": f"Cannot read {fp}"}
    s = max(1, int(sym.get("start_line") or 1))
    e = min(len(lines), int(sym.get("end_line") or s))
    snippet = "\n".join(f"{ln}: {lines[ln - 1]}" for ln in range(s, e + 1))
    return {
        "id": sym.get("id"),
        "file": sym.get("file"),
        "start_line": s,
        "end_line": e,
        "source": snippet,
    }


def dispatch_action(action: str, args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    if action == "index_folder":
        return index_folder(str(args.get("path") or os.getcwd()), env=env)

    if action == "list_repos":
        idir = index_dir(env)
        repos = []
        if idir.is_dir():
            for p in sorted(idir.glob("*.json")):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    repos.append({
                        "repo": d.get("repo", p.stem),
                        "source_root": d.get("source_root", ""),
                        "files": d.get("file_count", 0),
                        "symbols": d.get("symbol_count", 0),
                    })
                except Exception:
                    continue
        return {"repos": repos}

    repo_name = str(args.get("repo") or "")
    doc = _load_repo(repo_name, env=env)
    if doc is None:
        return {"error": f"Repository `{repo_name}` not indexed. Call `index_folder` first."}

    if action == "invalidate_cache":
        p = index_dir(env) / f"{doc['repo']}.json"
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
        return {"invalidated": doc["repo"]}

    if action == "get_file_tree":
        return {"repo": doc["repo"], "files": doc.get("source_files", [])}

    if action == "get_repo_outline":
        by_file: dict[str, list[str]] = {}
        for s in doc.get("symbols", []):
            by_file.setdefault(s["file"], []).append(s["name"])
        outline = [f"{f}: {', '.join(names[:8])}" for f, names in sorted(by_file.items())[:80]]
        return {"repo": doc["repo"], "files": doc.get("file_count", 0), "symbols": doc.get("symbol_count", 0), "outline": outline}

    if action == "get_file_outline":
        fp = str(args.get("file_path") or "").replace("\\", "/")
        syms = [s for s in doc.get("symbols", []) if s.get("file") == fp]
        return {"file": fp, "symbols": syms}

    if action == "search_symbols":
        q = str(args.get("query") or "").strip().lower()
        terms = [t for t in re.split(r"\s+", q) if t]
        scored: list[tuple[int, dict[str, Any]]] = []
        for s in doc.get("symbols", []):
            hay = f"{s.get('qualified_name','')} {s.get('signature','')} {s.get('file','')}".lower()
            score = sum(3 if t in s.get("name", "").lower() else (1 if t in hay else 0) for t in terms)
            if score > 0:
                scored.append((score, s))
        scored.sort(key=lambda x: (-x[0], x[1].get("file", ""), x[1].get("start_line", 0)))
        limit = int(args.get("limit") or 20)
        return {"repo": doc["repo"], "matches": [s for _, s in scored[:limit]]}

    if action == "get_symbol_source":
        sid = str(args.get("symbol_id") or "")
        for s in doc.get("symbols", []):
            if s.get("id") == sid or s.get("qualified_name") == sid:
                return _read_symbol_code(doc, s)
        return {"error": f"Symbol `{sid}` not found in `{doc['repo']}`."}

    if action == "get_symbols":
        ids = set(args.get("symbol_ids") or [])
        found = [_read_symbol_code(doc, s) for s in doc.get("symbols", []) if s.get("id") in ids]
        return {"symbols": found}

    if action == "search_text":
        q = str(args.get("query") or "")
        if not q:
            return {"matches": []}
        root = Path(doc["source_root"])
        low_q = q.lower()
        hits: list[dict[str, Any]] = []
        for rel in doc.get("source_files", []):
            try:
                lines = (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for idx, ln in enumerate(lines, start=1):
                if low_q in ln.lower():
                    hits.append({"file": rel, "line": idx, "text": ln.strip()[:160]})
                    if len(hits) >= 40:
                        return {"repo": doc["repo"], "matches": hits, "truncated": True}
        return {"repo": doc["repo"], "matches": hits}

    if action == "get_blast_radius":
        sym_name = str(args.get("symbol") or args.get("query") or "").strip()
        if not sym_name:
            return {"error": "Provide `symbol` name."}
        pat = re.compile(rf"\b{re.escape(sym_name)}\b")
        root = Path(doc["source_root"])
        impacted: list[dict[str, Any]] = []
        for rel in doc.get("source_files", []):
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            count = len(pat.findall(text))
            if count:
                impacted.append({"file": rel, "references": count})
        impacted.sort(key=lambda x: -x["references"])
        return {"repo": doc["repo"], "symbol": sym_name, "impacted_files": impacted[:30]}

    if action == "assemble_task_context":
        task = str(args.get("task") or "")
        budget = int(args.get("token_budget") or 4000)
        res = dispatch_action("search_symbols", {"repo": doc["repo"], "query": task, "limit": 12}, env=env)
        chunks = []
        used = 0
        for s in res.get("matches", []):
            code = _read_symbol_code(doc, s)
            toks = max(1, len(str(code.get("source") or "")) // 4)
            if used + toks > budget and chunks:
                break
            chunks.append(code)
            used += toks
        return {"repo": doc["repo"], "approx_tokens": used, "symbols": chunks}

    return {"error": f"Unknown action `{action}`. Call `menu` to list actions."}


# ---------------------------------------------------------------------------
# 6-Tool Counter Front Door MCP Schema
# ---------------------------------------------------------------------------

_MCP_TOOLS = [
    {
        "name": "menu",
        "description": "List available Terser AST index actions and exact argument shapes.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Optional filter keyword"}},
        },
    },
    {
        "name": "route",
        "description": "Suggest the best Terser AST index action for a coding task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task": {"type": "string"}, "repo": {"type": "string"}},
            "required": ["task"],
        },
    },
    {
        "name": "order",
        "description": "Execute any Terser AST index action (see `menu`).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "args": {"type": "object"},
                "allow_state_change": {"type": "boolean"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "index_folder",
        "description": "Index a local repository folder for symbol-first lookup.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "search_symbols",
        "description": "Search indexed functions, classes, structs, and methods by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["repo", "query"],
        },
    },
    {
        "name": "get_symbol_source",
        "description": "Fetch only the exact source lines for a symbol ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}, "symbol_id": {"type": "string"}},
            "required": ["repo", "symbol_id"],
        },
    },
]


def _handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "menu":
        q = str(arguments.get("query") or "").lower()
        items = {k: v for k, v in _ACTIONS_MENU.items() if not q or q in k.lower() or q in v["desc"].lower()}
        return {"actions": items}
    if name == "route":
        task = str(arguments.get("task") or "").lower()
        if any(k in task for k in ("blast", "impact", "reference", "caller")):
            rec = "get_blast_radius"
        elif any(k in task for k in ("outline", "structure", "map")):
            rec = "get_repo_outline"
        elif any(k in task for k in ("index", "scan")):
            rec = "index_folder"
        else:
            rec = "search_symbols"
        return {"recommended_action": rec, "args_hint": _ACTIONS_MENU[rec]["args"]}
    if name == "order":
        return dispatch_action(str(arguments.get("action") or ""), arguments.get("args") or {})
    return dispatch_action(name, arguments)


def _serve_stdio() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}

        if method == "notifications/initialized":
            continue
        if method == "initialize":
            result: Any = {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "tools/list":
            result = {"tools": _MCP_TOOLS}
        elif method == "tools/call":
            tool_name = str(params.get("name") or "")
            tool_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            payload = _handle_tool_call(tool_name, tool_args)
            result = {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}
        elif method == "ping":
            result = {}
        else:
            if req_id is None:
                continue
            resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue

        if req_id is not None:
            resp = {"jsonrpc": "2.0", "id": req_id, "result": result}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) >= 2 and args[0] == "index-file":
        reindex_file(args[1])
        return 0
    if len(args) >= 2 and args[0] == "index-folder":
        print(json.dumps(index_folder(args[1]), indent=2))
        return 0
    return _serve_stdio()


if __name__ == "__main__":
    sys.exit(main())
