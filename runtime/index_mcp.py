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
import sqlite3
import sys
import time
from contextlib import closing
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
    "search_sandbox": {"args": '{"archive_id": "--last", "query": "<terms>", "limit": 5}', "desc": "BM25 FTS5 search over an archived output or indexed doc"},
    "expand_sandbox": {"args": '{"archive_id": "--last", "start_line": 1, "end_line": 60}', "desc": "Read an exact line range from an archived output"},
    "index_sandbox_file": {"args": '{"path": "<abs-path>"}', "desc": "Index a large local doc or log into sandbox.db for BM25 search"},
    "execute_filtered": {"args": '{"argv": ["<cmd>", "..."], "intent": "<terms>", "cwd": "<dir>"}', "desc": "Run a safe command; index >5KB stdout into FTS5 and return BM25 matches"},
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


_BRACE_EXTS = frozenset({
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs",
    ".java", ".kt", ".kts", ".cs", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
    ".php", ".swift", ".scala", ".dart", ".zig",
})
_INDENT_EXTS = frozenset({".gd"})
_END_KW_EXTS = frozenset({".rb", ".lua"})
_END_OPEN_RE = re.compile(r"\b(?:def|class|module|function|if|unless|for|while|until|begin|case|do)\b")
_END_CLOSE_RE = re.compile(r"\bend\b")


def _find_block_end_line(lines: list[str], start_idx: int, ext: str, max_scan: int = 400) -> int:
    """Compute accurate 1-based end_line for non-Python symbols using brace/indent/end scanning."""
    total = len(lines)
    limit = min(total, start_idx + max_scan)

    if ext in _BRACE_EXTS:
        depth = 0
        seen_open = False
        in_block_comment = False
        for i in range(start_idx, limit):
            ln = lines[i]
            j, n = 0, len(ln)
            quote: str | None = None
            while j < n:
                ch = ln[j]
                nxt = ln[j + 1] if j + 1 < n else ""
                if in_block_comment:
                    if ch == "*" and nxt == "/":
                        in_block_comment = False
                        j += 2
                        continue
                    j += 1
                    continue
                if quote is not None:
                    if ch == "\\":
                        j += 2
                        continue
                    if ch == quote:
                        quote = None
                    j += 1
                    continue
                if ch == "/" and nxt == "/":
                    break
                if ch == "/" and nxt == "*":
                    in_block_comment = True
                    j += 2
                    continue
                if ch in ("'", '"', "`"):
                    quote = ch
                    j += 1
                    continue
                if ch == "{":
                    depth += 1
                    seen_open = True
                elif ch == "}":
                    if seen_open:
                        depth -= 1
                        if depth <= 0:
                            return i + 1
                elif ch == ";" and not seen_open and i >= start_idx:
                    return i + 1
                j += 1
        return min(total, start_idx + 40)

    if ext in _INDENT_EXTS:
        base_indent = len(lines[start_idx]) - len(lines[start_idx].lstrip(" \t"))
        end_i = start_idx
        for i in range(start_idx + 1, limit):
            s = lines[i].strip()
            if not s or s.startswith("#"):
                continue
            ind = len(lines[i]) - len(lines[i].lstrip(" \t"))
            if ind <= base_indent:
                break
            end_i = i
        return end_i + 1

    if ext in _END_KW_EXTS:
        depth = 0
        for i in range(start_idx, limit):
            cleaned = re.sub(r"(['\"]).*?\1", "", lines[i])
            cleaned = re.sub(r"(?:#|--).*$", "", cleaned)
            depth += len(_END_OPEN_RE.findall(cleaned))
            depth -= len(_END_CLOSE_RE.findall(cleaned))
            if i > start_idx and depth <= 0:
                return i + 1
        return min(total, start_idx + 40)

    return min(total, start_idx + 25)


def _extract_generic_symbols(rel_path: str, text: str) -> list[dict[str, Any]]:
    syms: list[dict[str, Any]] = []
    lines = text.splitlines()
    ext = Path(rel_path).suffix.lower()
    for idx, line in enumerate(lines, start=1):
        m = _GENERIC_SYM_RE.match(line)
        if not m:
            continue
        kind = m.group("kind")
        name = m.group("name")
        end_line = _find_block_end_line(lines, idx - 1, ext)
        syms.append({
            "id": f"{rel_path}::{name}@{idx}",
            "name": name,
            "qualified_name": name,
            "kind": kind,
            "file": rel_path,
            "start_line": idx,
            "end_line": end_line,
            "signature": line.strip()[:160],
            "doc": "",
        })
    return syms


_IMPORT_TOKEN_RE = re.compile(
    r"""(?m)^\s*(?:from\s+([\w.]+)\s+import\s+([\w,\s*]+)|import\s+([\w.,\s]+)|"""
    r"""(?:import|export).*?from\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)|"""
    r"""use\s+([\w:]+)|using\s+([\w.]+))"""
)


def _extract_imports(rel_path: str, text: str) -> set[str]:
    """Extract normalized imported module/symbol tokens for import-graph blast-radius precision."""
    tokens: set[str] = set()
    if Path(rel_path).suffix.lower() == ".py":
        try:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        for part in alias.name.split("."):
                            if len(part) >= 2:
                                tokens.add(part)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        for part in node.module.split("."):
                            if len(part) >= 2:
                                tokens.add(part)
                    for alias in node.names:
                        if alias.name != "*" and len(alias.name) >= 2:
                            tokens.add(alias.name)
            return tokens
        except SyntaxError:
            pass
    for m in _IMPORT_TOKEN_RE.finditer(text):
        for g in m.groups():
            if not g:
                continue
            for tok in re.split(r"[/\\.:,\s]+", g):
                if tok and len(tok) >= 2 and tok not in (".", "..", "as"):
                    tokens.add(tok)
    return tokens


def _db_path(slug: str, env: dict[str, str] | None = None) -> Path:
    return index_dir(env) / f"{slug}.db"


def _init_index_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS symbols (
            id TEXT PRIMARY KEY,
            name TEXT,
            qualified_name TEXT,
            kind TEXT,
            file TEXT,
            start_line INTEGER,
            end_line INTEGER,
            signature TEXT,
            doc TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sym_file ON symbols(file)")
    conn.execute("CREATE TABLE IF NOT EXISTS imports (src_file TEXT, token TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_imp_token ON imports(token)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_imp_src ON imports(src_file)")


def _write_index_db(
    slug: str,
    source_root: str,
    files: list[str],
    symbols: list[dict[str, Any]],
    import_edges: list[tuple[str, str]],
    *,
    env: dict[str, str] | None = None,
) -> None:
    dbp = _db_path(slug, env)
    try:
        dbp.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(dbp), timeout=5.0)) as conn:
            _init_index_db(conn)
            with conn:
                conn.execute("DELETE FROM files")
                conn.execute("DELETE FROM symbols")
                conn.execute("DELETE FROM imports")
                conn.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", [
                    ("repo", slug),
                    ("source_root", source_root),
                    ("indexed_at", str(round(time.time(), 1))),
                ])
                conn.executemany("INSERT OR IGNORE INTO files(path) VALUES (?)", [(f,) for f in files])
                conn.executemany(
                    "INSERT OR REPLACE INTO symbols VALUES (?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            s.get("id", ""), s.get("name", ""), s.get("qualified_name", ""),
                            s.get("kind", ""), s.get("file", ""), int(s.get("start_line") or 1),
                            int(s.get("end_line") or 1), s.get("signature", ""), s.get("doc", ""),
                        )
                        for s in symbols
                    ],
                )
                conn.executemany("INSERT INTO imports(src_file, token) VALUES (?, ?)", import_edges)
    except sqlite3.Error:
        pass


def _update_index_db_file(
    slug: str,
    rel: str,
    exists: bool,
    syms: list[dict[str, Any]],
    imp_tokens: set[str],
    *,
    env: dict[str, str] | None = None,
) -> None:
    dbp = _db_path(slug, env)
    if not dbp.is_file():
        return
    try:
        with closing(sqlite3.connect(str(dbp), timeout=5.0)) as conn:
            _init_index_db(conn)
            with conn:
                conn.execute("DELETE FROM symbols WHERE file = ?", (rel,))
                conn.execute("DELETE FROM imports WHERE src_file = ?", (rel,))
                if exists:
                    conn.execute("INSERT OR IGNORE INTO files(path) VALUES (?)", (rel,))
                    conn.executemany(
                        "INSERT OR REPLACE INTO symbols VALUES (?,?,?,?,?,?,?,?,?)",
                        [
                            (
                                s.get("id", ""), s.get("name", ""), s.get("qualified_name", ""),
                                s.get("kind", ""), s.get("file", ""), int(s.get("start_line") or 1),
                                int(s.get("end_line") or 1), s.get("signature", ""), s.get("doc", ""),
                            )
                            for s in syms
                        ],
                    )
                    conn.executemany("INSERT INTO imports(src_file, token) VALUES (?, ?)", [(rel, t) for t in imp_tokens])
                else:
                    conn.execute("DELETE FROM files WHERE path = ?", (rel,))
    except sqlite3.Error:
        pass


def _query_db_importers(slug: str, tokens: set[str], *, env: dict[str, str] | None = None) -> set[str]:
    dbp = _db_path(slug, env)
    if not tokens or not dbp.is_file():
        return set()
    try:
        with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
            qmarks = ",".join("?" for _ in tokens)
            rows = conn.execute(
                f"SELECT DISTINCT src_file FROM imports WHERE token IN ({qmarks})",
                list(tokens),
            ).fetchall()
            return {str(r[0]) for r in rows if r and r[0]}
    except sqlite3.Error:
        return set()


def _index_single_file_with_imports(root: Path, file_path: Path) -> tuple[list[dict[str, Any]], set[str]]:
    try:
        st = file_path.stat()
        if not file_path.is_file() or st.st_size > _MAX_FILE_BYTES:
            return [], set()
        raw = file_path.read_bytes()
        if b"\x00" in raw[:2048]:
            return [], set()
        text = raw.decode("utf-8", errors="replace")
    except OSError:
        return [], set()
    rel = file_path.relative_to(root).as_posix()
    syms = _extract_python_symbols(rel, text) if file_path.suffix.lower() == ".py" else _extract_generic_symbols(rel, text)
    return syms, _extract_imports(rel, text)


def _index_single_file(root: Path, file_path: Path) -> list[dict[str, Any]]:
    syms, _ = _index_single_file_with_imports(root, file_path)
    return syms


def index_folder(path: str, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    root = Path(os.path.expanduser(path)).resolve()
    if not root.is_dir():
        return {"error": f"Directory not found: {path}"}

    slug = _repo_slug(root)
    files: list[str] = []
    symbols: list[dict[str, Any]] = []
    import_edges: list[tuple[str, str]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for fname in sorted(filenames):
            fp = Path(dirpath) / fname
            if fp.suffix.lower() not in _CODE_EXTS:
                continue
            rel = fp.relative_to(root).as_posix()
            files.append(rel)
            f_syms, f_imps = _index_single_file_with_imports(root, fp)
            symbols.extend(f_syms)
            for tok in f_imps:
                import_edges.append((rel, tok))

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
    _write_index_db(slug, str(root), files, symbols, import_edges, env=env)
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
        new_syms: list[dict[str, Any]] = []
        new_imps: set[str] = set()
        if fp.is_file():
            files.add(rel)
            new_syms, new_imps = _index_single_file_with_imports(root, fp)
            syms.extend(new_syms)
        else:
            files.discard(rel)
        data["source_files"] = sorted(files)
        data["symbols"] = syms
        data["file_count"] = len(files)
        data["symbol_count"] = len(syms)
        data["indexed_at"] = round(time.time(), 1)
        atomic_write_json(meta_file, data)
        _update_index_db_file(str(data.get("repo") or meta_file.stem), rel, fp.is_file(), new_syms, new_imps, env=env)
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


def _exec_filtered(args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run a safe argv command without shell=True; index >5KB stdout into FTS5 when intent is set."""
    import shlex
    import subprocess
    from runtime import context_sandbox
    from runtime.config import no_window_kwargs, record_savings

    raw_argv = args.get("argv")
    if isinstance(raw_argv, list) and raw_argv:
        argv = [str(x) for x in raw_argv if str(x)]
    elif isinstance(args.get("command"), str) and args["command"].strip():
        try:
            argv = shlex.split(args["command"])
        except ValueError as exc:
            return {"error": f"Invalid command syntax: {exc}"}
    else:
        return {"error": "Provide `argv` (list of strings) or `command`."}

    if not argv:
        return {"error": "Empty command."}
    intent = str(args.get("intent") or "").strip()
    cwd = str(args.get("cwd") or "").strip() or None
    timeout = max(1, min(60, int(args.get("timeout") or 30)))
    limit = max(1, min(10, int(args.get("limit") or 5)))

    try:
        proc = subprocess.run(
            argv,
            shell=False,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **no_window_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s", "argv": argv}
    except OSError as exc:
        return {"error": f"Failed to execute {argv[0]}: {exc}"}

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    cmd_label = " ".join(argv)[:160]

    # Tee-on-failure: preserve error output verbatim (archive if huge)
    if proc.returncode != 0:
        out: dict[str, Any] = {
            "returncode": proc.returncode,
            "stdout": stdout[:8192],
            "stderr": stderr[:8192],
        }
        if len(stdout) + len(stderr) > 8192:
            aid = context_sandbox.store_output(stdout + "\n" + stderr, command=cmd_label, env=env)
            if aid:
                out["archive_id"] = aid
        return out

    # Intent-driven filtering when stdout > 5 KB
    if len(stdout) > 5120 and intent:
        aid = context_sandbox.store_output(stdout, command=cmd_label, env=env)
        if aid:
            matched = context_sandbox.query_output(aid, intent, limit=limit, env=env) or ""
            record_savings(
                "fts_sandbox",
                len(stdout),
                len(matched),
                host=os.environ.get("TERSER_CLIENT_ID", "mcp"),
                detail=argv[0],
                env=env,
            )
            return {
                "returncode": 0,
                "filtered": True,
                "archive_id": aid,
                "raw_bytes": len(stdout),
                "output": matched,
                "stderr": stderr[:1024] if stderr else "",
            }

    return {
        "returncode": 0,
        "filtered": False,
        "raw_bytes": len(stdout),
        "output": stdout[:16384],
        "stderr": stderr[:1024] if stderr else "",
    }


def dispatch_action(action: str, args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    if action == "search_sandbox":
        from runtime import context_sandbox

        aid = str(args.get("archive_id") or "--last")
        query = str(args.get("query") or "").strip()
        limit = max(1, min(15, int(args.get("limit") or 5)))
        res = context_sandbox.query_output(aid, query, limit=limit, env=env)
        if res is None:
            return {"error": f"Archive `{aid}` not found or query empty."}
        return {"archive_id": aid, "result": res}

    if action == "expand_sandbox":
        from runtime import context_sandbox

        aid = str(args.get("archive_id") or "--last")
        s_ln = int(args["start_line"]) if args.get("start_line") is not None else None
        e_ln = int(args["end_line"]) if args.get("end_line") is not None else None
        res = context_sandbox.expand_output(aid, start_line=s_ln, end_line=e_ln, env=env)
        if res is None:
            return {"error": f"Archive `{aid}` not found."}
        return {"archive_id": aid, "content": res}

    if action == "index_sandbox_file":
        from runtime import context_sandbox

        path = str(args.get("path") or "").strip()
        if not path:
            return {"error": "Missing `path`."}
        return context_sandbox.index_file(path, env=env)

    if action == "execute_filtered":
        return _exec_filtered(args, env=env)

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
        for ext in (".json", ".db", ".db-wal", ".db-shm"):
            p = index_dir(env) / f"{doc['repo']}{ext}"
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
        call_pat = re.compile(rf"(?:\.|::|\b){re.escape(sym_name)}\s*\(")
        root = Path(doc["source_root"])

        # Use SQLite import-edge graph to prioritize true importers and suppress incidental word-match noise
        def_files = {str(s.get("file")) for s in doc.get("symbols", []) if s.get("name") == sym_name}
        imp_tokens: set[str] = {sym_name}
        for df in def_files:
            stem = Path(df).stem
            if stem not in ("__init__", "index", "mod", "main"):
                imp_tokens.add(stem)
        importers = _query_db_importers(doc["repo"], imp_tokens, env=env)
        has_graph = bool(importers)

        impacted: list[dict[str, Any]] = []
        for rel in doc.get("source_files", []):
            is_def = rel in def_files
            is_imp = rel in importers
            try:
                text = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            count = len(pat.findall(text))
            if not count:
                continue
            # When import-graph edges exist, skip non-importing files unless they explicitly invoke the symbol
            if has_graph and not is_def and not is_imp and not call_pat.search(text):
                continue
            entry: dict[str, Any] = {"file": rel, "references": count}
            if is_def:
                entry["defines"] = True
            if is_imp:
                entry["importer"] = True
            impacted.append(entry)
        impacted.sort(key=lambda x: (not x.get("defines", False), not x.get("importer", False), -x["references"]))
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
        "description": "List Terser index actions and arg shapes.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    },
    {
        "name": "route",
        "description": "Recommend best index action for a task.",
        "inputSchema": {
            "type": "object",
            "properties": {"task": {"type": "string"}, "repo": {"type": "string"}},
            "required": ["task"],
        },
    },
    {
        "name": "order",
        "description": "Run any Terser index action (see `menu`).",
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
        "description": "Index a local repo folder.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "search_symbols",
        "description": "Search indexed symbols by name.",
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
        "description": "Fetch exact source lines for a symbol ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}, "symbol_id": {"type": "string"}},
            "required": ["repo", "symbol_id"],
        },
    },
]


def format_mcp_response(action: str, payload: dict[str, Any]) -> str:
    """Serialize MCP tool responses into compact, lossless text (~40-60% fewer tokens than indented JSON)."""
    if not isinstance(payload, dict) or "error" in payload:
        return json.dumps(payload, separators=(",", ":"))

    if action == "search_symbols" and isinstance(payload.get("matches"), list):
        matches = payload["matches"]
        if not matches:
            return f"repo={payload.get('repo','')} matches=0"
        lines = [f"repo={payload.get('repo','')} ({len(matches)} matches):"]
        for s in matches:
            doc = f" # {s['doc']}" if s.get("doc") else ""
            lines.append(f"{s.get('id')} | L{s.get('start_line')}-{s.get('end_line')} | {s.get('signature','')}{doc}")
        return "\n".join(lines)

    if action == "get_file_outline" and isinstance(payload.get("symbols"), list):
        syms = payload["symbols"]
        lines = [f"file={payload.get('file','')} ({len(syms)} symbols):"]
        for s in syms:
            doc = f" # {s['doc']}" if s.get("doc") else ""
            lines.append(f"{s.get('id')} | L{s.get('start_line')}-{s.get('end_line')} | {s.get('signature','')}{doc}")
        return "\n".join(lines)

    if action == "search_text" and isinstance(payload.get("matches"), list):
        matches = payload["matches"]
        if not matches:
            return f"repo={payload.get('repo','')} matches=0"
        grouped: dict[str, list[str]] = {}
        for m in matches:
            grouped.setdefault(str(m.get("file", "")), []).append(f"  {m.get('line')}: {m.get('text','')}")
        lines = [f"repo={payload.get('repo','')} ({len(matches)} hits{' truncated' if payload.get('truncated') else ''}):"]
        for fpath, hits in grouped.items():
            lines.append(f"{fpath}:")
            lines.extend(hits)
        return "\n".join(lines)

    if action == "get_blast_radius" and isinstance(payload.get("impacted_files"), list):
        imp = payload["impacted_files"]
        lines = [f"repo={payload.get('repo','')} symbol={payload.get('symbol','')} ({len(imp)} files):"]
        for item in imp:
            lines.append(f"  {item.get('file')} ({item.get('references')} refs)")
        return "\n".join(lines)

    if action == "get_file_tree" and isinstance(payload.get("files"), list):
        files = payload["files"]
        return f"repo={payload.get('repo','')} ({len(files)} files):\n" + "\n".join(files)

    if action == "get_symbol_source" and isinstance(payload.get("source"), str):
        return f"{payload.get('id')} ({payload.get('file')} L{payload.get('start_line')}-{payload.get('end_line')}):\n{payload['source']}"

    if action in ("get_symbols", "assemble_task_context") and isinstance(payload.get("symbols"), list):
        blocks: list[str] = []
        if action == "assemble_task_context":
            blocks.append(f"repo={payload.get('repo','')} (~{payload.get('approx_tokens', 0)} tok, {len(payload['symbols'])} symbols):")
        for s in payload["symbols"]:
            if isinstance(s, dict) and isinstance(s.get("source"), str):
                blocks.append(f"--- {s.get('id')} ({s.get('file')} L{s.get('start_line')}-{s.get('end_line')}) ---\n{s['source']}")
        if blocks:
            return "\n".join(blocks)

    return json.dumps(payload, separators=(",", ":"))


def _handle_tool_call(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if name == "menu":
        q = str(arguments.get("query") or "").lower()
        items = {k: v for k, v in _ACTIONS_MENU.items() if not q or q in k.lower() or q in v["desc"].lower()}
        return "menu", {"actions": items}
    if name == "route":
        task = str(arguments.get("task") or "").lower()
        if any(k in task for k in ("sandbox", "archive", "log output")):
            rec = "search_sandbox"
        elif any(k in task for k in ("execute", "run command", "filter output")):
            rec = "execute_filtered"
        elif any(k in task for k in ("blast", "impact", "reference", "caller")):
            rec = "get_blast_radius"
        elif any(k in task for k in ("outline", "structure", "map")):
            rec = "get_repo_outline"
        elif any(k in task for k in ("index", "scan")):
            rec = "index_folder"
        else:
            rec = "search_symbols"
        return "route", {"recommended_action": rec, "args_hint": _ACTIONS_MENU[rec]["args"]}
    if name == "order":
        act = str(arguments.get("action") or "")
        return act, dispatch_action(act, arguments.get("args") or {})
    return name, dispatch_action(name, arguments)


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
            resolved_action, payload = _handle_tool_call(tool_name, tool_args)
            result = {"content": [{"type": "text", "text": format_mcp_response(resolved_action, payload)}]}
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
