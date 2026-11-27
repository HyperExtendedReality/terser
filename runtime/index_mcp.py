#!/usr/bin/env python3
"""Terser — Self-Contained Stdlib AST & Symbol Code-Index MCP Server (`terser-index`).

Zero required dependencies (pure Python stdlib; `tree-sitter-language-pack` is used for exact
non-Python symbol spans when installed). Implements:
  - JSON-RPC 2.0 MCP over stdio (6-tool Counter front door: `menu`, `order`, `route`,
    `index_folder`, `search_symbols`, `get_symbol_source`)
  - Full AST parsing for Python (`ast`) + structural regex indexing for 25+ languages
    (JS/TS/Rust/Go/Java/Kotlin/C#/C++/Ruby/PHP/Swift/Lua/Zig/GDScript)
  - Persistent local index under `~/.terser/code-index/<repo>.json`
  - Incremental single-file reindex CLI: `python runtime/index_mcp.py index-file <path>`
  - Incremental folder re-index (stat-matched row reuse) and a lossless FTS5 trigram
    pre-filter for text search, both stored in the per-repo SQLite index
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
from functools import lru_cache
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
    # Extended languages (structural regex per language, see _EXTRA_SYM_PATTERNS)
    ".ex", ".exs", ".erl", ".hrl", ".hs", ".ml", ".mli", ".fs", ".fsx", ".clj", ".cljs", ".cljc",
    ".lisp", ".el", ".scm", ".r", ".jl", ".nim", ".cr", ".sol", ".proto", ".graphql", ".gql",
    ".tf", ".hcl", ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".pl", ".pm", ".groovy", ".gradle",
    ".hx", ".d", ".pas", ".pp", ".f90", ".f95", ".vue", ".svelte", ".mm", ".elm", ".vb", ".cmake",
    ".m", ".cu", ".cuh", ".hlsl", ".glsl", ".wgsl", ".v", ".sv", ".vhd", ".vhdl", ".odin", ".mojo",
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
    "get_symbol_source": {"args": '{"repo": "<name>", "symbol_id": "<id>", "start_line": 1, "end_line": 20}', "desc": "Fetch exact symbol source. In Codex order.args, reuse_visible_result=true permits reuse of a verified same-turn result"},
    "get_symbols": {"args": '{"repo": "<name>", "symbol_ids": ["<id>"]}', "desc": "Fetch exact source for multiple symbols"},
    "search_text": {"args": '{"repo": "<name>", "query": "<text>"}', "desc": "Fast text search across indexed files"},
    "get_blast_radius": {"args": '{"repo": "<name>", "symbol": "<name>"}', "desc": "Find files referencing a symbol"},
    "assemble_task_context": {"args": '{"repo": "<name>", "task": "<desc>", "token_budget": 4000}', "desc": "Assemble top matching symbols within a token budget"},
    "invalidate_cache": {"args": '{"repo": "<name>"}', "desc": "Delete stored index for a repository"},
    "search_sandbox": {"args": '{"archive_id": "--last", "query": "<terms>", "limit": 5}', "desc": "BM25 FTS5 search over an archived output or indexed doc"},
    "expand_sandbox": {"args": '{"archive_id": "--last", "start_line": 1, "end_line": 60}', "desc": "Read an exact line range from an archived output"},
    "index_sandbox_file": {"args": '{"path": "<abs-path>"}', "desc": "Index a large local doc or log into sandbox.db for BM25 search"},
    "execute_batch": {"args": '{"commands": ["git log --oneline -50", ["npm", "ls"]], "queries": ["<terms>"], "cwd": "<dir>"}', "desc": "Run several safe commands in one call; large outputs are indexed and only lines matching the queries come back"},
    "execute_filtered": {"args": '{"argv": ["<cmd>", "..."], "intent": "<terms>", "cwd": "<dir>"}', "desc": "Run a safe command; index >5KB stdout into FTS5 and return BM25 matches"},
    "purge_sandbox": {"args": '{"archive_id": "<id>"} | {"session": "<id>"} | {"all": true}', "desc": "Delete archived outputs by archive id, by session, or all"},
    "find_references": {"args": '{"repo": "<name>", "symbol": "<name>"}', "desc": "Every line where a name is used (definition lines excluded)"},
    "find_importers": {"args": '{"repo": "<name>", "module": "<dotted.name|rel/path>"}', "desc": "Files that import a module"},
    "get_call_hierarchy": {"args": '{"repo": "<name>", "symbol": "<name|id>", "depth": 2}', "desc": "Callers and callees of a symbol, up to 4 levels"},
    "check_edit_safe": {"args": '{"repo": "<name>", "symbol": "<name>"}', "desc": "SAFE/CAUTION/RISKY verdict for editing a symbol, with affected files"},
    "check_delete_safe": {"args": '{"repo": "<name>", "symbol": "<name>"}', "desc": "SAFE/UNSAFE verdict for deleting a symbol, with affected files"},
    "check_rename_safe": {"args": '{"repo": "<name>", "symbol": "<name>", "new_name": "<name>"}', "desc": "SAFE/REVIEW/CONFLICT verdict for a rename, with files to update"},
    "get_changed_symbols": {"args": '{"repo": "<name>", "ref": "HEAD", "staged": false}', "desc": "Map git diff hunks (and untracked files) to symbols"},
    "find_dead_code": {"args": '{"repo": "<name>", "limit": 50, "include_tests": false}', "desc": "Symbols whose name is never referenced"},
    "get_dependency_cycles": {"args": '{"repo": "<name>"}', "desc": "Import cycles between indexed files"},
    # Code Mode (runtime/code_mode.py): work runs locally, only results come back
    "run_script": {"args": '{"code": "for h in t.grep(r\\"TODO\\", \\"*.py\\"): print(h)", "language": "python", "apply": false, "intent": "<terms>"}', "desc": "Run a local python/node/bash/powershell script; only stdout returns. Python gets `t` helpers (files, grep, count, symbols, source, refs, read, json, toml, csv, replace, sub, write, edit_symbol); edits are a diff until apply=true"},
    "code_search": {"args": '{"all": ["<regex>"], "any": [], "none": ["<regex>"], "glob": "*.ts", "mode": "files|count|hits"}', "desc": "One-call multi-pattern search: files matching ALL/ANY/NONE of several regexes"},
    "code_edit": {"args": '{"edits": [{"path": "a.py", "old": "x", "new": "y"}, {"glob": "*.py", "pattern": "re", "repl": "r"}, {"symbol_id": "<id>", "source": "<code>"}], "apply": true}', "desc": "Batch exact/regex/whole-symbol edits across files, all-or-nothing; diff when apply=false, diffstat when applied"},
    "code_query": {"args": '{"path": "package.json", "select": "dependencies.*"} | {"path": "pyproject.toml", "outline": true}', "desc": "Select values from JSON/JSONL/TOML/CSV/INI by dotted path, or outline the keys"},
    "fetch_and_index": {"args": '{"url": "https://...", "query": "<terms>"}', "desc": "Fetch a web page, archive it in sandbox.db, return its outline plus the sections matching query"},
    # Repository analysis (runtime/index_analysis.py); any graph/analysis action accepts path_prefix
    "get_hotspots": {"args": '{"repo": "<name>", "days": 90, "limit": 20}', "desc": "Files ranked by git churn x size"},
    "get_symbol_history": {"args": '{"repo": "<name>", "symbol": "<name|id>"}', "desc": "Commits that touched one symbol's lines"},
    "get_untested_symbols": {"args": '{"repo": "<name>", "include_private": false}', "desc": "Functions/classes never named in any test file"},
    "get_class_hierarchy": {"args": '{"repo": "<name>", "symbol": "<class>"}', "desc": "Ancestors and descendants of a class, interface, or trait"},
    "find_implementations": {"args": '{"repo": "<name>", "symbol": "<interface|trait>"}', "desc": "Classes and impls that extend or implement a type"},
    "find_similar_symbols": {"args": '{"repo": "<name>", "threshold": 0.8, "symbol": "<optional name|id>", "structural": false}', "desc": "Near-duplicate functions (MinHash token shingles)"},
    "get_symbol_diff": {"args": '{"repo": "<name>", "symbol": "<name|id>", "ref": "HEAD"}', "desc": "One symbol's diff between a git ref and the working tree"},
    "get_pr_risk_profile": {"args": '{"repo": "<name>", "base": "origin/main"}', "desc": "Changed symbols scored by blast radius, tests, churn, and size"},
    "get_layer_violations": {"args": '{"repo": "<name>", "layers": [{"name": "ui", "paths": ["src/ui/**"]}, {"name": "core", "paths": ["src/core/**"]}]}', "desc": "Imports that break a declared layer order (or [[layers]] in .terser/config.toml)"},
    "list_workspaces": {"args": '{"repo": "<name>"}', "desc": "Monorepo members (npm/pnpm/yarn/lerna, Cargo, go.work, uv) with index counts"},
    "import_scip": {"args": '{"repo": "<name>", "path": "index.scip"}', "desc": "Load a SCIP index so find_references returns compiler-precise results"},
}

_CODE_MODE_ACTIONS = frozenset({"run_script", "code_search", "code_edit", "code_query"})
_ANALYSIS_ACTIONS = frozenset({
    "get_hotspots", "get_symbol_history", "get_untested_symbols", "get_class_hierarchy",
    "find_implementations", "find_similar_symbols", "get_symbol_diff", "get_pr_risk_profile",
    "get_layer_violations", "list_workspaces", "import_scip",
})


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


def _add_import_tokens(node: ast.AST, tokens: set[str]) -> None:
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


def _collect_stmt_imports(root: ast.AST, tokens: set[str]) -> None:
    """Collect imports reachable from `root` by visiting statements only.

    Import nodes are statements, and statements only live in body/orelse/finalbody,
    exception-handler bodies, and match-case bodies, so skipping expression nodes gives
    the same tokens as `ast.walk` at a fraction of the cost.
    """
    stack: list[ast.AST] = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _add_import_tokens(node, tokens)
            continue
        for field in ("body", "orelse", "finalbody"):
            val = getattr(node, field, None)
            if isinstance(val, list):
                stack.extend(x for x in val if isinstance(x, ast.stmt))
        for handler in getattr(node, "handlers", None) or ():
            stack.extend(handler.body)
        for case in getattr(node, "cases", None) or ():
            stack.extend(case.body)


def _extract_python_symbols_and_imports(rel_path: str, text: str) -> tuple[list[dict[str, Any]], set[str]]:
    """Single-pass Python AST extractor for both symbols and imported module/symbol tokens."""
    syms: list[dict[str, Any]] = []
    tokens: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _extract_generic_symbols(rel_path, text), _extract_imports(rel_path, text)

    lines = text.splitlines()

    def _visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "class" if isinstance(child, ast.ClassDef) else ("method" if prefix else "function")
                qual = f"{prefix}.{child.name}" if prefix else child.name
                start = getattr(child, "lineno", 1)
                end = getattr(child, "end_lineno", start) or start
                sig = lines[start - 1].strip() if 0 <= start - 1 < len(lines) else child.name
                doc_raw = ast.get_docstring(child)
                doc = doc_raw.splitlines()[0][:120] if doc_raw else ""
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
                else:
                    # Local imports inside functions/methods, collected in the same pass
                    _collect_stmt_imports(child, tokens)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                _add_import_tokens(child, tokens)
            elif isinstance(child, ast.stmt):
                _collect_stmt_imports(child, tokens)

    _visit(tree)
    return syms, tokens


def _extract_python_symbols(rel_path: str, text: str) -> list[dict[str, Any]]:
    syms, _ = _extract_python_symbols_and_imports(rel_path, text)
    return syms


_BRACE_EXTS = frozenset({
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs",
    ".java", ".kt", ".kts", ".cs", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
    ".php", ".swift", ".scala", ".dart", ".zig",
    ".sol", ".proto", ".graphql", ".gql", ".tf", ".hcl", ".groovy", ".gradle", ".hx", ".d",
    ".r", ".pl", ".pm", ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".vue", ".svelte", ".mm",
    ".cu", ".cuh", ".hlsl", ".glsl", ".wgsl", ".odin", ".m",
})
_INDENT_EXTS = frozenset({".gd", ".nim", ".fs", ".fsx", ".elm", ".hs", ".mojo"})
_END_KW_EXTS = frozenset({".rb", ".lua", ".jl", ".cr"})
_DO_END_EXTS = frozenset({".ex", ".exs"})
_END_OPEN_RE = re.compile(r"\b(?:def|class|module|function|if|unless|for|while|until|begin|case|do)\b")
_END_CLOSE_RE = re.compile(r"\bend\b")
_DO_OPEN_RE = re.compile(r"\bdo\b(?!:)|\bfn\b")

# Per-language structural patterns for the extended extensions. Each regex names `name`
# (or `name2`) and optionally `kind`; a missing kind defaults to "function".
_I = re.IGNORECASE
_EXTRA_SYM_PATTERNS: dict[str, list[re.Pattern[str]]] = {}


def _extra(exts: str, *patterns: str, flags: int = 0) -> None:
    compiled = [re.compile(p, flags) for p in patterns]
    for ext in exts.split():
        _EXTRA_SYM_PATTERNS.setdefault(ext, []).extend(compiled)


_extra(".ex .exs", r"^\s*(?P<kind>defmodule|defprotocol|defimpl|defmacrop?|defp?|defstruct|defguardp?)\s+(?P<name>[A-Za-z_][\w.?!]*)")
_extra(".erl .hrl", r"^(?P<name>[a-z]\w*)\s*\(.*\)\s*(?:when\s.*)?->", r"^-(?P<kind>record|type|spec)\s*\(\s*(?P<name>[a-z]\w*)")
_extra(".hs", r"^(?P<kind>data|newtype|class|instance|type)\s+(?:\([^)]*\)\s*=>\s*)?(?P<name>[A-Z][\w']*)", r"^(?P<name>[a-z_][\w']*)\s*::")
_extra(".ml .mli", r"^\s*(?P<kind>let(?: rec)?|and|module(?: type)?|type|val|class|exception)\s+(?P<name>[A-Za-z_][\w']*)")
_extra(".fs .fsx", r"^\s*(?P<kind>let(?: rec| inline| private| mutable)*|member|type|module|exception)\s+(?:private\s+|internal\s+|this\.|_\.|__\.)?(?P<name>[A-Za-z_][\w']*)")
_extra(".clj .cljs .cljc", r"^\s*\((?P<kind>defn-?|defmacro|defprotocol|defrecord|deftype|defmulti|defmethod|defonce|def|ns)\s+(?:\^\S+\s+)?(?P<name>[^\s()\[\]{}]+)")
_extra(".lisp .el .scm", r"^\s*\((?P<kind>defun|defmacro|defvar|defparameter|defconst|defclass|defgeneric|defmethod|defstruct|define-syntax|define)\s+\(?(?P<name>[^\s()]+)")
_extra(".r", r"^\s*(?P<name>[A-Za-z_.][\w.]*)\s*(?:<-|<<-|=)\s*(?P<kind>function)\b", r"^\s*(?P<kind>setClass|setGeneric|R6Class)\s*\(\s*[\"'](?P<name>[\w.]+)")
_extra(".jl", r"^\s*(?:@\w+\s+)?(?P<kind>function|macro|struct|mutable struct|abstract type|primitive type|module|baremodule)\s+(?P<name>[A-Za-z_][\w!]*)",
       r"^\s*(?P<name>[A-Za-z_][\w!]*)\s*\([^)]*\)\s*=[^=]")
_extra(".nim", r"^\s*(?P<kind>proc|func|method|iterator|template|macro|converter)\s+`?(?P<name>[A-Za-z_]\w*)", r"^\s*(?P<name>[A-Z]\w*)\*?\s*(?:\[[^\]]*\])?\s*=\s*(?:ref\s+|ptr\s+)?(?P<kind>object|enum|tuple|concept|distinct)")
_extra(".cr", r"^\s*(?:private\s+|protected\s+|abstract\s+)*(?P<kind>def|class|struct|module|enum|macro|lib|annotation)\s+(?P<name>[A-Za-z_][\w.?!]*)")
_extra(".sol", r"^\s*(?:abstract\s+)?(?P<kind>contract|library|interface|function|modifier|event|struct|enum|error)\s+(?P<name>[A-Za-z_]\w*)")
_extra(".proto", r"^\s*(?P<kind>message|service|rpc|enum|extend)\s+(?P<name>[A-Za-z_][\w.]*)")
_extra(".graphql .gql", r"^\s*(?:extend\s+)?(?P<kind>type|input|interface|enum|union|scalar|query|mutation|subscription|fragment|directive)\s+@?(?P<name>[A-Za-z_]\w*)")
_extra(".tf .hcl", r'^\s*(?P<kind>resource|data|module|variable|output|provider)\s+"(?P<name>[^"]+)"(?:\s+"(?P<sub>[^"]+)")?')
_extra(".sh .bash .zsh", r"^\s*(?P<kind>function)\s+(?P<name>[A-Za-z_][\w:.-]*)", r"^\s*(?P<name>[A-Za-z_][\w:.-]*)\s*\(\)\s*(?:\{|$)")
_extra(".ps1 .psm1", r"^\s*(?P<kind>function|filter|class|enum|workflow|configuration)\s+(?P<name>[A-Za-z_][\w-]*)", flags=_I)
_extra(".pl .pm", r"^\s*(?P<kind>sub|package)\s+(?P<name>[A-Za-z_][\w:]*)")
_extra(".pas .pp", r"^\s*(?:class\s+)?(?P<kind>procedure|function|constructor|destructor|program|unit)\s+(?P<name>[A-Za-z_][\w.]*)", r"^\s*(?P<name>T[A-Za-z_]\w*)\s*=\s*(?P<kind>class|record|interface|object)\b", flags=_I)
_extra(".f90 .f95", r"^\s*(?:pure\s+|elemental\s+|recursive\s+|impure\s+|module\s+)*(?:(?:integer|real|logical|character|complex|double precision|type\([^)]*\))\s+)?(?P<kind>subroutine|function|module|program|submodule)\s+(?P<name>[A-Za-z_]\w*)", r"^\s*(?P<kind>type)\s*(?:,[^:]*)?::\s*(?P<name>[A-Za-z_]\w*)", flags=_I)
_extra(".mm .m", r"^\s*(?P<kind>@interface|@implementation|@protocol)\s+(?P<name>[A-Za-z_]\w*)", r"^[-+]\s*\([^)]*\)\s*(?P<name>[A-Za-z_]\w*)")
_extra(".elm", r"^(?P<kind>type alias|type|port module|port|module)\s+(?P<name>[A-Za-z][\w.]*)", r"^(?P<name>[a-z]\w*)\s*:")
_extra(".vb", r"^\s*(?:(?:Public|Private|Friend|Protected|Shared|Overrides|Overridable|MustOverride|NotOverridable|Async|Partial|ReadOnly|Static)\s+)*(?P<kind>Sub|Function|Class|Module|Structure|Interface|Enum|Property|Namespace)\s+(?P<name>[A-Za-z_]\w*)", flags=_I)
_extra(".cmake", r"^\s*(?P<kind>function|macro)\s*\(\s*(?P<name>[\w-]+)", flags=_I)
_extra(".cu .cuh .hlsl .glsl .wgsl", r"^\s*(?:(?:__global__|__device__|__host__|static|inline)\s+)*(?:[\w:<>]+\s+){1,3}\**(?!(?:if|for|while|switch|return|else|do|case)\b)(?P<name>[A-Za-z_]\w*)\s*\([^;]*$", r"^\s*(?P<kind>fn|struct)\s+(?P<name>[A-Za-z_]\w*)")
_extra(".v .sv", r"^\s*(?P<kind>module|interface|package|class|function|task|program)\s+(?:automatic\s+|static\s+)?(?:[\w\[\]:]+\s+)?(?P<name>[A-Za-z_]\w*)\s*[(#;]")
_extra(".vhd .vhdl", r"^\s*(?P<kind>entity|architecture|package|function|procedure|component)\s+(?:body\s+)?(?P<name>[A-Za-z_]\w*)", flags=_I)
_extra(".odin", r"^\s*(?P<name>[A-Za-z_]\w*)\s*::\s*(?P<kind>proc|struct|enum|union)\b")
_extra(".vue .svelte", r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?P<kind>function|class)\s+(?P<name>[A-Za-z_$][\w$]*)", r"^\s*(?:export\s+)?const\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>")


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

    if ext in _END_KW_EXTS or ext in _DO_END_EXTS:
        open_re = _DO_OPEN_RE if ext in _DO_END_EXTS else _END_OPEN_RE
        depth = 0
        for i in range(start_idx, limit):
            cleaned = re.sub(r"(['\"]).*?\1", "", lines[i])
            cleaned = re.sub(r"(?:#|--).*$", "", cleaned)
            if ext in _DO_END_EXTS and i == start_idx and not open_re.search(cleaned):
                return i + 1  # one-line `def x(a), do: ...`
            depth += len(open_re.findall(cleaned))
            depth -= len(_END_CLOSE_RE.findall(cleaned))
            if i > start_idx and depth <= 0:
                return i + 1
        return min(total, start_idx + 40)

    return min(total, start_idx + 25)


# Optional exact-span extraction via tree-sitter (`pip install tree-sitter-language-pack`).
# Falls back to the structural regex when the package or a grammar is unavailable.
_TS_LANG_BY_EXT = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".rs": "rust", ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin", ".cs": "csharp", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".scala": "scala", ".lua": "lua",
}

# node type -> reported kind; containers are descended into, other symbols are not.
_TS_KINDS = {
    "function_declaration": "function", "generator_function_declaration": "function",
    "function_definition": "function", "function_item": "fn", "method_declaration": "method",
    "method_definition": "method", "method": "method", "singleton_method": "method",
    "constructor_declaration": "constructor", "class_declaration": "class", "class_definition": "class",
    "class_specifier": "class", "class": "class", "interface_declaration": "interface",
    "protocol_declaration": "interface", "struct_item": "struct", "struct_specifier": "struct",
    "struct_declaration": "struct", "enum_item": "enum", "enum_declaration": "enum",
    "enum_specifier": "enum", "trait_item": "trait", "trait_definition": "trait", "impl_item": "impl",
    "mod_item": "module", "module": "module", "namespace_definition": "namespace",
    "namespace_declaration": "namespace", "object_declaration": "object", "object_definition": "object",
    "type_alias_declaration": "type", "type_item": "type", "type_spec": "type",
}
_TS_CONTAINERS = frozenset({
    "class_declaration", "class_definition", "class_specifier", "class", "interface_declaration",
    "protocol_declaration", "struct_item", "trait_item", "trait_definition", "impl_item", "mod_item",
    "module", "namespace_definition", "namespace_declaration", "object_declaration", "object_definition",
    "struct_declaration", "enum_declaration",
})
_TS_NAME_NODES = frozenset({
    "identifier", "type_identifier", "field_identifier", "property_identifier", "constant",
    "simple_identifier", "name", "qualified_identifier", "scoped_identifier", "destructor_name",
    "operator_name",
})
_TS_PARSERS: dict[str, Any] = {}
_TS_DISABLED = object()


def _ts_parser(lang: str) -> Any:
    cached = _TS_PARSERS.get(lang)
    if cached is not None:
        return None if cached is _TS_DISABLED else cached
    parser: Any = _TS_DISABLED
    if not os.environ.get("TERSER_DISABLE_TREE_SITTER"):
        try:
            from tree_sitter_language_pack import get_parser  # type: ignore[import-not-found]

            parser = get_parser(lang)
        except Exception:
            parser = _TS_DISABLED
    _TS_PARSERS[lang] = parser
    return None if parser is _TS_DISABLED else parser


def _ts_text(node: Any, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _ts_name(node: Any, src: bytes) -> str:
    field = "type" if node.type == "impl_item" else "name"
    target = node.child_by_field_name(field)
    if target is None:
        # C/C++ functions: follow the declarator chain to the identifier.
        decl = node.child_by_field_name("declarator")
        hops = 0
        while decl is not None and decl.type not in _TS_NAME_NODES and hops < 8:
            decl = decl.child_by_field_name("declarator") or (decl.named_children[0] if decl.named_children else None)
            hops += 1
        target = decl
    if target is None:
        for child in node.named_children:
            if child.type in _TS_NAME_NODES:
                target = child
                break
    if target is None:
        return ""
    return re.sub(r"\s+", "", _ts_text(target, src))[:120]


def _extract_treesitter_symbols(rel_path: str, text: str) -> list[dict[str, Any]] | None:
    lang = _TS_LANG_BY_EXT.get(Path(rel_path).suffix.lower())
    if not lang:
        return None
    parser = _ts_parser(lang)
    if parser is None:
        return None
    try:
        src = text.encode("utf-8")
        tree = parser.parse(src)
        lines = text.splitlines()
        syms: list[dict[str, Any]] = []
        stack: list[tuple[Any, str]] = [(tree.root_node, "")]
        while stack:
            node, prefix = stack.pop()
            for child in reversed(node.named_children):
                kind = _TS_KINDS.get(child.type)
                if kind is None:
                    if child.named_child_count and child.type not in ("block", "statement_block", "compound_statement"):
                        stack.append((child, prefix))
                    continue
                name = _ts_name(child, src)
                if not name:
                    continue
                start = child.start_point[0] + 1
                end = max(start, child.end_point[0] + 1)
                qual = f"{prefix}.{name}" if prefix else name
                sig = lines[start - 1].strip()[:160] if 0 < start <= len(lines) else ""
                syms.append({
                    "id": f"{rel_path}::{qual}@{start}",
                    "name": name.rsplit("::", 1)[-1].rsplit(".", 1)[-1],
                    "qualified_name": qual,
                    "kind": kind,
                    "file": rel_path,
                    "start_line": start,
                    "end_line": end,
                    "signature": sig,
                    "doc": "",
                })
                if child.type in _TS_CONTAINERS:
                    stack.append((child, qual))
        syms.sort(key=lambda x: (x["start_line"], x["end_line"]))
        return syms
    except Exception:
        return None


def _extract_generic_symbols(rel_path: str, text: str) -> list[dict[str, Any]]:
    ts_syms = _extract_treesitter_symbols(rel_path, text)
    if ts_syms is not None:
        return ts_syms
    syms: list[dict[str, Any]] = []
    lines = text.splitlines()
    ext = Path(rel_path).suffix.lower()
    extra = _EXTRA_SYM_PATTERNS.get(ext)
    for idx, line in enumerate(lines, start=1):
        kind = name = ""
        if extra:
            for rx in extra:
                em = rx.match(line)
                if em:
                    gd = em.groupdict()
                    name = gd.get("name") or gd.get("name2") or ""
                    if gd.get("sub"):
                        name = f"{name}.{gd['sub']}"
                    kind = (gd.get("kind") or "function").split()[-1].lstrip("@").lower()
                    break
        if not name:
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


def _meta_path(slug: str, env: dict[str, str] | None = None) -> Path:
    return index_dir(env) / f"{slug}.meta.json"


def _resolve_repo_slug(repo: str, *, env: dict[str, str] | None = None) -> tuple[str, dict[str, Any]] | None:
    """Resolve `repo` name/prefix to `(slug, meta_dict)` using tiny `.meta.json` (O(1)).

    When `repo` is empty, auto-resolves from `cwd` (matching `source_root`) or the sole indexed repo.
    """
    idir = index_dir(env)
    if not idir.is_dir():
        return None
    if not repo:
        norm_cwd = normalize_workspace(os.getcwd())
        all_metas: list[tuple[str, dict[str, Any]]] = []
        for mp in sorted(idir.glob("*.meta.json")):
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
                if not isinstance(m, dict) or not m.get("source_root"):
                    continue
                slug = str(m.get("repo") or mp.name[:-10])
                sr = normalize_workspace(str(m.get("source_root") or ""))
                if norm_cwd and sr and (norm_cwd == sr or norm_cwd.startswith(sr + "/") or sr.startswith(norm_cwd + "/")):
                    return slug, m
                all_metas.append((slug, m))
            except Exception:
                continue
        if len(all_metas) == 1:
            return all_metas[0]
        return None
    low = repo.lower()
    # 1. Exact or prefix match against tiny .meta.json files
    exact_meta = idir / f"{repo}.meta.json"
    candidates = [exact_meta] if exact_meta.is_file() else []
    if not candidates:
        for p in sorted(idir.glob("*.meta.json")):
            slug = p.name[:-10]  # strip ".meta.json"
            if slug.lower() == low or slug.lower().startswith(low + "-"):
                candidates.append(p)
                break
    for mp in candidates:
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
            if isinstance(m, dict) and m.get("source_root"):
                return str(m.get("repo") or mp.name[:-10]), m
        except Exception:
            continue
    return None


def repo_slug_for_path(path: str, *, env: dict[str, str] | None = None) -> str:
    """Slug of the indexed repo whose source root contains (or equals) ``path``; '' if none."""
    idir = index_dir(env)
    target = normalize_workspace(path)
    if not target or not idir.is_dir():
        return ""
    best = ("", 0)
    for mp in idir.glob("*.meta.json"):
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            continue
        sr = normalize_workspace(str(m.get("source_root") or "")) if isinstance(m, dict) else ""
        if sr and (target == sr or target.startswith(sr + "/")) and len(sr) > best[1]:
            best = (str(m.get("repo") or mp.name[:-10]), len(sr))
    return best[0]


def _db_query_file_outline(slug: str, fp: str, *, env: dict[str, str] | None = None) -> list[dict[str, Any]] | None:
    dbp = _db_path(slug, env)
    if not dbp.is_file():
        return None
    try:
        with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, name, qualified_name, kind, file, start_line, end_line, signature, doc "
                "FROM symbols WHERE file = ? ORDER BY start_line ASC",
                (fp,),
            ).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error:
        return None


def _db_query_search_symbols(slug: str, terms: list[str], limit: int, *, env: dict[str, str] | None = None) -> list[dict[str, Any]] | None:
    dbp = _db_path(slug, env)
    if not dbp.is_file():
        return None
    try:
        with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
            conn.row_factory = sqlite3.Row
            if not terms:
                return []
            where = " OR ".join(
                "(lower(name) LIKE ? OR lower(qualified_name) LIKE ? OR lower(signature) LIKE ? OR lower(file) LIKE ?)"
                for _ in terms
            )
            params: list[str] = []
            for t in terms:
                like = f"%{t}%"
                params.extend([like, like, like, like])
            rows = conn.execute(
                f"SELECT id, name, qualified_name, kind, file, start_line, end_line, signature, doc FROM symbols WHERE {where}",
                params,
            ).fetchall()
            scored: list[tuple[int, dict[str, Any]]] = []
            for r in rows:
                s = dict(r)
                hay = f"{s.get('qualified_name','')} {s.get('signature','')} {s.get('file','')}".lower()
                score = sum(3 if t in s.get("name", "").lower() else (1 if t in hay else 0) for t in terms)
                if score > 0:
                    scored.append((score, s))
            scored.sort(key=lambda x: (-x[0], x[1].get("file", ""), x[1].get("start_line", 0)))
            return [s for _, s in scored[:limit]]
    except sqlite3.Error:
        return None


def _db_query_symbol_by_id(slug: str, sid: str, *, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    dbp = _db_path(slug, env)
    if not dbp.is_file():
        return None
    try:
        with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, name, qualified_name, kind, file, start_line, end_line, signature, doc "
                "FROM symbols WHERE id = ? OR qualified_name = ? LIMIT 1",
                (sid, sid),
            ).fetchone()
            return dict(row) if row else None
    except sqlite3.Error:
        return None


def _db_query_files(slug: str, *, env: dict[str, str] | None = None) -> list[str] | None:
    dbp = _db_path(slug, env)
    if not dbp.is_file():
        return None
    try:
        with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
            rows = conn.execute("SELECT path FROM files ORDER BY path ASC").fetchall()
            return [str(r[0]) for r in rows]
    except sqlite3.Error:
        return None


def _db_query_symbols_by_ids(slug: str, ids: list[str], *, env: dict[str, str] | None = None) -> list[dict[str, Any]] | None:
    dbp = _db_path(slug, env)
    if not dbp.is_file():
        return None
    if not ids:
        return []
    try:
        with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
            conn.row_factory = sqlite3.Row
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT id, name, qualified_name, kind, file, start_line, end_line, signature, doc "
                f"FROM symbols WHERE id IN ({placeholders}) ORDER BY file ASC, start_line ASC",
                ids,
            ).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error:
        return None


def _db_query_repo_outline(slug: str, *, env: dict[str, str] | None = None) -> list[str] | None:
    dbp = _db_path(slug, env)
    if not dbp.is_file():
        return None
    try:
        with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
            rows = conn.execute(
                "SELECT file, name FROM symbols ORDER BY file ASC, start_line ASC"
            ).fetchall()
            by_file: dict[str, list[str]] = {}
            for fp, name in rows:
                lst = by_file.setdefault(str(fp), [])
                if len(lst) < 8:
                    lst.append(str(name))
            return [f"{f}: {', '.join(names)}" for f, names in sorted(by_file.items())[:80]]
    except sqlite3.Error:
        return None


def _db_query_def_files(slug: str, sym_name: str, *, env: dict[str, str] | None = None) -> set[str] | None:
    dbp = _db_path(slug, env)
    if not dbp.is_file():
        return None
    try:
        with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
            rows = conn.execute("SELECT DISTINCT file FROM symbols WHERE name = ?", (sym_name,)).fetchall()
            return {str(r[0]) for r in rows}
    except sqlite3.Error:
        return None


def _init_index_db(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS file_digests (path TEXT PRIMARY KEY, digest TEXT NOT NULL)")
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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS file_stats (path TEXT PRIMARY KEY, mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL)"
    )
    _ensure_content_fts(conn)


def _ensure_content_fts(conn: sqlite3.Connection) -> bool:
    """Create the trigram content table used to pre-filter text searches (SQLite >= 3.34).

    Bodies are stored lower-cased so `LIKE` candidates are a strict superset of the
    case-insensitive line matches `search_text` verifies against the files on disk.
    """
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS content_fts USING fts5(path UNINDEXED, body, tokenize='trigram')"
        )
        return True
    except sqlite3.Error:
        return False


def _content_fts_ready(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_fts'"
        ).fetchone()
        return bool(row)
    except sqlite3.Error:
        return False


def _write_index_db(
    slug: str,
    source_root: str,
    files: list[str],
    symbols: list[dict[str, Any]],
    import_edges: list[tuple[str, str]],
    file_digests: list[tuple[str, str]],
    *,
    file_stats: list[tuple[str, int, int]] | None = None,
    bodies: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Write the repo index. `bodies` maps rel path -> lower-cased text for changed files only;
    unchanged files keep their existing trigram rows."""
    dbp = _db_path(slug, env)
    try:
        dbp.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(str(dbp), timeout=5.0)) as conn:
            _init_index_db(conn)
            fts = _content_fts_ready(conn)
            with conn:
                conn.execute("DELETE FROM files")
                conn.execute("DELETE FROM file_digests")
                conn.execute("DELETE FROM symbols")
                conn.execute("DELETE FROM imports")
                conn.execute("DELETE FROM file_stats")
                if file_stats:
                    conn.executemany("INSERT OR REPLACE INTO file_stats VALUES (?,?,?)", file_stats)
                if fts:
                    keep = set(files)
                    stale = [
                        r[0] for r in conn.execute("SELECT DISTINCT path FROM content_fts").fetchall()
                        if r[0] not in keep or (bodies is not None and r[0] in bodies)
                    ]
                    conn.executemany("DELETE FROM content_fts WHERE path = ?", [(x,) for x in stale])
                    if bodies:
                        conn.executemany(
                            "INSERT INTO content_fts(path, body) VALUES (?, ?)",
                            sorted(bodies.items()),
                        )
                conn.executemany("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", [
                    ("repo", slug),
                    ("source_root", source_root),
                    ("indexed_at", str(round(time.time(), 1))),
                    ("extractor", _extractor_tag()),
                ])
                conn.executemany("INSERT OR IGNORE INTO files(path) VALUES (?)", [(f,) for f in files])
                conn.executemany("INSERT INTO file_digests VALUES (?,?)", file_digests)
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
    digest: str,
    *,
    stat: tuple[int, int] | None = None,
    body: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    dbp = _db_path(slug, env)
    if not dbp.is_file():
        return
    try:
        with closing(sqlite3.connect(str(dbp), timeout=5.0)) as conn:
            _init_index_db(conn)
            fts = _content_fts_ready(conn)
            with conn:
                conn.execute("DELETE FROM symbols WHERE file = ?", (rel,))
                conn.execute("DELETE FROM imports WHERE src_file = ?", (rel,))
                conn.execute("DELETE FROM file_digests WHERE path = ?", (rel,))
                conn.execute("DELETE FROM file_stats WHERE path = ?", (rel,))
                if fts:
                    conn.execute("DELETE FROM content_fts WHERE path = ?", (rel,))
                if exists:
                    conn.execute("INSERT OR IGNORE INTO files(path) VALUES (?)", (rel,))
                    if digest:
                        conn.execute("INSERT INTO file_digests VALUES (?,?)", (rel, digest))
                    # Only record a stat when the body is stored too, so a stat match always
                    # implies the trigram row reflects that exact file version.
                    if stat is not None and (body is not None or not fts):
                        conn.execute("INSERT INTO file_stats VALUES (?,?,?)", (rel, stat[0], stat[1]))
                    if fts and body is not None:
                        conn.execute("INSERT INTO content_fts(path, body) VALUES (?, ?)", (rel, body))
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


def _text_candidates(
    slug: str, root: Path, files: list[str], needle: str, *, env: dict[str, str] | None = None,
) -> list[str]:
    """Return `files` (order kept) that may contain `needle` (case-insensitive).

    Lossless pre-filter: a file is skipped only when its on-disk (mtime_ns, size) still
    equals the indexed version AND the lower-cased trigram body lacks the needle.
    Anything unindexed, changed, or unverifiable is always kept for a real scan.
    """
    low = needle.lower()
    if len(low) < 3 or not files:
        return files
    dbp = _db_path(slug, env)
    if not dbp.is_file():
        return files
    try:
        with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
            if not _content_fts_ready(conn):
                return files
            stats = {
                str(r[0]): (int(r[1]), int(r[2]))
                for r in conn.execute("SELECT path, mtime_ns, size FROM file_stats").fetchall()
            }
            phrase = '"' + low.replace('"', '""') + '"'
            matched = {str(r[0]) for r in conn.execute(
                "SELECT path FROM content_fts WHERE content_fts MATCH ?", (phrase,)
            ).fetchall()}
    except sqlite3.Error:
        return files
    out: list[str] = []
    for rel in files:
        if rel in matched:
            out.append(rel)
            continue
        known = stats.get(rel)
        if known is None:
            out.append(rel)
            continue
        try:
            st = (root / rel).stat()
        except OSError:
            continue  # Unreadable files never produce matches in the full scan either.
        if (st.st_mtime_ns, st.st_size) != known:
            out.append(rel)
    return out


def _read_indexable_file(file_path: Path) -> bytes:
    with file_path.open("rb") as handle:
        raw = handle.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES or b"\x00" in raw[:2048]:
        raise ValueError("File is oversized or binary")
    return raw


def _index_single_file_with_imports(root: Path, file_path: Path) -> tuple[list[dict[str, Any]], set[str], str]:
    try:
        if not file_path.is_file():
            return [], set(), ""
        raw = _read_indexable_file(file_path)
    except (OSError, ValueError):
        return [], set(), ""
    rel = file_path.relative_to(root).as_posix()
    _, symbols, imports = _source_snapshot(rel, raw)
    return symbols, imports, hashlib.sha256(raw).hexdigest()


def _index_single_file(root: Path, file_path: Path) -> list[dict[str, Any]]:
    syms, _, _ = _index_single_file_with_imports(root, file_path)
    return syms


_PARALLEL_MIN_FILES = 48
# Bump when symbol/import extraction output changes, so incremental re-index reparses everything.
_EXTRACTOR_VERSION = 1


def _extractor_tag() -> str:
    has_ts = False
    if not os.environ.get("TERSER_DISABLE_TREE_SITTER"):
        try:
            import importlib.util

            has_ts = importlib.util.find_spec("tree_sitter_language_pack") is not None
        except (ImportError, ValueError):
            has_ts = False
    return f"v{_EXTRACTOR_VERSION}-{'ts' if has_ts else 're'}"


def _parse_job(job: tuple[str, str]) -> tuple[str, list[dict[str, Any]], list[str], str, tuple[int, int] | None, str | None]:
    """Parse one file (top-level so it can run in a worker process)."""
    root_s, rel = job
    fp = Path(root_s) / rel
    try:
        st = fp.stat()
        raw = _read_indexable_file(fp)
    except (OSError, ValueError):
        return rel, [], [], "", None, None
    _, symbols, imports = _source_snapshot(rel, raw)
    body = raw.decode("utf-8", errors="replace").lower()
    return rel, list(symbols), sorted(imports), hashlib.sha256(raw).hexdigest(), (st.st_mtime_ns, st.st_size), body


def _index_workers(job_count: int, env: dict[str, str] | None) -> int:
    """Worker processes for parsing. Opt-in via TERSER_INDEX_WORKERS: measured on a
    1.5k-file repo, process start-up and result pickling cost more than parsing saves,
    and `spawn` re-imports the caller's __main__ module in every worker."""
    e = env if env is not None else os.environ
    raw = str(e.get("TERSER_INDEX_WORKERS") or "").strip()
    if raw.isdigit() and job_count >= _PARALLEL_MIN_FILES:
        return max(1, min(32, int(raw)))
    return 1


def _run_parse_jobs(root: Path, rels: list[str], env: dict[str, str] | None) -> list[tuple]:
    jobs = [(str(root), rel) for rel in rels]
    workers = _index_workers(len(jobs), env)
    if workers > 1 and len(jobs) > 1:
        try:
            from concurrent.futures import ProcessPoolExecutor

            with ProcessPoolExecutor(max_workers=workers) as pool:
                return list(pool.map(_parse_job, jobs, chunksize=16))
        except Exception:
            pass  # Spawn-restricted hosts fall back to serial parsing.
    return [_parse_job(j) for j in jobs]


def _skip_rel(rel: str) -> bool:
    parts = rel.split("/")
    return any(part in _SKIP_DIRS or part.startswith(".") for part in parts[:-1])


def _list_source_files(root: Path, _depth: int = 0) -> list[str]:
    """Source files under ``root`` minus project `[index] skip` globs and `.contextignore` matches."""
    files = _list_source_files_raw(root, _depth)
    if _depth:
        return files
    try:
        from runtime import index_analysis, user_filters

        skip, _ = index_analysis.index_rules(root)
        has_ignore = user_filters.ignore_present(str(root))
    except Exception:
        return files
    if not skip and not has_ignore:
        return files
    return [
        f for f in files
        if not any(index_analysis.glob_match(f, g) for g in skip)
        and not (has_ignore and user_filters.ignored_by(str(root / f), str(root)))
    ]


def _project_exts(root: Path) -> frozenset[str]:
    try:
        from runtime import index_analysis

        return frozenset(index_analysis.index_rules(root)[1])
    except Exception:
        return frozenset()


def _list_source_files_raw(root: Path, _depth: int = 0) -> list[str]:
    """List candidate source files, honoring .gitignore when `root` is inside a git work tree.

    Submodules / nested repos show up in `git ls-files` as a single directory entry; they are
    listed recursively so their code stays indexed exactly as the plain directory walk did.
    """
    extra_exts = _project_exts(root)
    try:
        import subprocess

        from runtime.config import no_window_kwargs

        proc = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20,
            **no_window_kwargs(),
        )
        if proc.returncode == 0:
            out: set[str] = set()
            for item in proc.stdout.decode("utf-8", errors="replace").split("\0"):
                rel = item.strip().rstrip("/")
                if not rel or _skip_rel(rel + "/x"):
                    continue
                full = root / rel
                if full.is_dir():
                    if _depth < 4 and not Path(rel).name.startswith("."):
                        out.update(f"{rel}/{sub}" for sub in _list_source_files(full, _depth + 1))
                    continue
                if (Path(rel).suffix.lower() in _CODE_EXTS or Path(rel).suffix.lower() in extra_exts) and full.is_file():
                    out.add(rel)
            return sorted(out)
    except Exception:
        pass
    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for fname in sorted(filenames):
            fp = Path(dirpath) / fname
            if fp.suffix.lower() in _CODE_EXTS or fp.suffix.lower() in extra_exts:
                files.append(fp.relative_to(root).as_posix())
    return sorted(files)


def _load_existing_rows(slug: str, env: dict[str, str] | None) -> dict[str, dict[str, Any]]:
    """Existing per-file rows (stat, digest, symbols, imports) for incremental re-indexing."""
    dbp = _db_path(slug, env)
    if not dbp.is_file():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    try:
        with closing(sqlite3.connect(str(dbp), timeout=5.0)) as conn:
            conn.row_factory = sqlite3.Row
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if not {"file_stats", "file_digests", "symbols", "imports", "meta"} <= names:
                return {}
            tag = conn.execute("SELECT v FROM meta WHERE k = 'extractor'").fetchone()
            if not tag or tag[0] != _extractor_tag():
                return {}  # Different extractor -> reparse everything for consistent symbols.
            for r in conn.execute(
                "SELECT s.path, s.mtime_ns, s.size, d.digest FROM file_stats s JOIN file_digests d ON d.path = s.path"
            ).fetchall():
                rows[r["path"]] = {
                    "stat": (int(r["mtime_ns"]), int(r["size"])),
                    "digest": r["digest"],
                    "symbols": [],
                    "imports": [],
                }
            for r in conn.execute(
                "SELECT id, name, qualified_name, kind, file, start_line, end_line, signature, doc FROM symbols"
            ).fetchall():
                ent = rows.get(r["file"])
                if ent is not None:
                    ent["symbols"].append(dict(r))
            for r in conn.execute("SELECT src_file, token FROM imports").fetchall():
                ent = rows.get(r["src_file"])
                if ent is not None:
                    ent["imports"].append(r["token"])
    except sqlite3.Error:
        return {}
    return rows


def index_folder(path: str, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    root = Path(os.path.expanduser(path)).resolve()
    if not root.is_dir():
        return {"error": f"Directory not found: {path}"}

    slug = _repo_slug(root)
    rels = _list_source_files(root)
    existing = _load_existing_rows(slug, env)

    # Incremental: reuse rows for files whose (mtime_ns, size) still match the indexed version.
    reused: dict[str, dict[str, Any]] = {}
    to_parse: list[str] = []
    for rel in rels:
        prev = existing.get(rel)
        if prev is not None:
            try:
                st = (root / rel).stat()
            except OSError:
                continue
            if prev["stat"] == (st.st_mtime_ns, st.st_size):
                reused[rel] = prev
                continue
        to_parse.append(rel)

    parsed = {res[0]: res for res in _run_parse_jobs(root, to_parse, env)}

    files: list[str] = []
    symbols: list[dict[str, Any]] = []
    import_edges: list[tuple[str, str]] = []
    file_digests: list[tuple[str, str]] = []
    file_stats: list[tuple[str, int, int]] = []
    bodies: dict[str, str] = {}
    for rel in rels:
        if rel in reused:
            prev = reused[rel]
            files.append(rel)
            symbols.extend(prev["symbols"])
            import_edges.extend((rel, tok) for tok in prev["imports"])
            file_digests.append((rel, prev["digest"]))
            file_stats.append((rel, prev["stat"][0], prev["stat"][1]))
            continue
        res = parsed.get(rel)
        if res is None:
            continue
        _, f_syms, f_imps, digest, stat, body = res
        files.append(rel)
        symbols.extend(f_syms)
        import_edges.extend((rel, tok) for tok in f_imps)
        if digest:
            file_digests.append((rel, digest))
        if stat is not None and body is not None:
            file_stats.append((rel, stat[0], stat[1]))
            bodies[rel] = body

    meta = {
        "repo": slug,
        "source_root": str(root),
        "indexed_at": round(time.time(), 1),
        "file_count": len(files),
        "symbol_count": len(symbols),
    }
    _write_index_db(
        slug, str(root), files, symbols, import_edges, file_digests,
        file_stats=file_stats, bodies=bodies, env=env,
    )
    atomic_write_json(_meta_path(slug, env), meta)
    # Remove any legacy monolithic `<slug>.json` now superseded by `.db` + `.meta.json`
    try:
        (index_dir(env) / f"{slug}.json").unlink(missing_ok=True)
    except OSError:
        pass
    return {
        "repo": slug,
        "source_root": str(root),
        "files": len(files),
        "symbols": len(symbols),
        "reparsed": len(to_parse),
        "reused": len(reused),
    }


def _load_repo(repo: str, *, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    idir = index_dir(env)
    if not idir.is_dir():
        return None
    # Prefer SQLite + tiny .meta.json (fast, O(1) metadata)
    resolved = _resolve_repo_slug(repo, env=env)
    if resolved is not None:
        slug, meta = resolved
        dbp = _db_path(slug, env)
        if dbp.is_file():
            try:
                with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
                    conn.row_factory = sqlite3.Row
                    files = [str(r[0]) for r in conn.execute("SELECT path FROM files ORDER BY path ASC").fetchall()]
                    syms = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT id, name, qualified_name, kind, file, start_line, end_line, signature, doc FROM symbols"
                        ).fetchall()
                    ]
                return {
                    "repo": slug,
                    "source_root": meta.get("source_root", ""),
                    "indexed_at": meta.get("indexed_at", 0),
                    "file_count": len(files),
                    "symbol_count": len(syms),
                    "source_files": files,
                    "symbols": syms,
                }
            except sqlite3.Error:
                pass
    # Legacy .json fallback
    if not repo:
        norm_cwd = normalize_workspace(os.getcwd())
        legacy_docs: list[dict[str, Any]] = []
        for p in sorted(idir.glob("*.json")):
            if p.name.endswith(".meta.json"):
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(d, dict):
                    continue
                sr = normalize_workspace(str(d.get("source_root") or ""))
                if norm_cwd and sr and (norm_cwd == sr or norm_cwd.startswith(sr + "/") or sr.startswith(norm_cwd + "/")):
                    return d
                legacy_docs.append(d)
            except Exception:
                continue
        return legacy_docs[0] if len(legacy_docs) == 1 else None
    direct = idir / f"{repo}.json"
    if direct.is_file():
        try:
            return json.loads(direct.read_text(encoding="utf-8"))
        except Exception:
            return None
    low = repo.lower()
    for p in idir.glob("*.json"):
        if p.name.endswith(".meta.json"):
            continue
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

    # Fast O(1) SQLite + .meta.json path (avoids reading/rewriting multi-MB repo JSON)
    for meta_file in idir.glob("*.meta.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        sr = str(meta.get("source_root") or "")
        norm_sr = normalize_workspace(sr)
        if not norm_sr or not norm_fp.startswith(norm_sr + "/"):
            continue
        slug = str(meta.get("repo") or meta_file.name[:-10])
        dbp = _db_path(slug, env)
        if not dbp.is_file():
            continue
        root = Path(sr)
        try:
            rel = fp.relative_to(root).as_posix()
        except ValueError:
            continue
        _, j_syms, j_imps, digest, j_stat, j_body = _parse_job((str(root), rel))
        _update_index_db_file(
            slug, rel, fp.is_file(), j_syms, set(j_imps), digest, stat=j_stat, body=j_body, env=env,
        )
        try:
            with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
                f_cnt = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
                s_cnt = int(conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
            meta["file_count"] = f_cnt
            meta["symbol_count"] = s_cnt
            meta["indexed_at"] = round(time.time(), 1)
            atomic_write_json(meta_file, meta)
        except sqlite3.Error:
            pass
        return True

    # Legacy .json fallback
    for meta_file in idir.glob("*.json"):
        if meta_file.name.endswith(".meta.json"):
            continue
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
        new_syms_leg: list[dict[str, Any]] = []
        new_imps_leg: set[str] = set()
        digest_leg = ""
        if fp.is_file():
            files.add(rel)
            new_syms_leg, new_imps_leg, digest_leg = _index_single_file_with_imports(root, fp)
            syms.extend(new_syms_leg)
        else:
            files.discard(rel)
        data["source_files"] = sorted(files)
        data["symbols"] = syms
        data["file_count"] = len(files)
        data["symbol_count"] = len(syms)
        data["indexed_at"] = round(time.time(), 1)
        atomic_write_json(meta_file, data)
        _update_index_db_file(str(data.get("repo") or meta_file.stem), rel, fp.is_file(), new_syms_leg, new_imps_leg, digest_leg, env=env)
        return True
    return False


@lru_cache(maxsize=16)
def _source_snapshot(rel: str, raw: bytes) -> tuple[list[str], list[dict[str, Any]], set[str]]:
    text = raw.decode("utf-8", errors="replace")
    if Path(rel).suffix.lower() == ".py":
        symbols, imports = _extract_python_symbols_and_imports(rel, text)
    else:
        symbols, imports = _extract_generic_symbols(rel, text), _extract_imports(rel, text)
    return text.splitlines(), symbols, imports


def _read_symbol_code(
    repo_doc: dict[str, Any],
    sym: dict[str, Any],
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    env: dict[str, str] | None = None,
    snapshots: dict[Path, bytes] | None = None,
) -> dict[str, Any]:
    root = Path(str(repo_doc.get("source_root") or ""))
    rel = str(sym.get("file") or "")
    fp = root / rel
    try:
        raw = snapshots.get(fp) if snapshots is not None else None
        if raw is None:
            raw = _read_indexable_file(fp)
            if snapshots is not None:
                snapshots[fp] = raw
    except OSError:
        return {"error": f"Cannot read {fp}"}
    except ValueError:
        return {"error": f"Cannot verify indexed source for {rel}. Read the file directly."}
    lines, symbols, imports = _source_snapshot(rel, raw)
    slug = str(repo_doc.get("repo") or "")
    digest = hashlib.sha256(raw).hexdigest()
    if fp.suffix.lower() == ".py":
        matches = [current for current in symbols if current["id"] == sym.get("id")]
    else:
        identity = ("qualified_name", "kind", "signature")
        matches = [current for current in symbols if all(current[key] == sym.get(key) for key in identity)]
        # Generic IDs use a line number. A moved declaration can be recovered
        # by a unique signature, but identical declarations are ambiguous.
    if len(matches) > 1:
        dbp = _db_path(slug, env)
        fingerprint = None
        if slug and dbp.is_file():
            try:
                with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
                    fingerprint = conn.execute("SELECT digest FROM file_digests WHERE path = ?", (rel,)).fetchone()
            except sqlite3.Error:
                pass  # Legacy databases have no recorded digest.
        matches = [m for m in matches if m == sym] if fingerprint == (digest,) else []
        if len(matches) != 1:
            return {"error": f"Ambiguous symbol in {rel}. Refresh the index or read the exact range."}
    if len(matches) != 1:
        return {"error": f"Symbol `{sym.get('id')}` changed in {rel}. Refresh the index or read the file directly."}
    current = matches[0]
    if current != sym and slug:
        try:
            cur_st = fp.stat()
            cur_stat: tuple[int, int] | None = (cur_st.st_mtime_ns, cur_st.st_size) if cur_st.st_size == len(raw) else None
        except OSError:
            cur_stat = None
        _update_index_db_file(
            slug, rel, True, symbols, imports, digest, stat=cur_stat,
            body=raw.decode("utf-8", errors="replace").lower() if cur_stat else None, env=env,
        )
    s = max(current["start_line"], start_line or 1)
    e = min(current["end_line"], end_line if end_line is not None else len(lines), len(lines))
    if e < s:
        return {"error": f"Requested range does not overlap `{current['id']}` (lines {current['start_line']}-{current['end_line']})."}
    snippet = "\n".join(f"{ln}: {lines[ln - 1]}" for ln in range(s, e + 1))
    return {
        "id": current["id"],
        "file": rel,
        "start_line": s,
        "end_line": e,
        "source": snippet,
        "_revision": digest,
    }


def _assemble_sources(
    repo_doc: dict[str, Any], matches: list[dict[str, Any]], budget: int,
    *, env: dict[str, str] | None = None,
) -> dict[str, Any]:
    snapshots: dict[Path, bytes] = {}
    chunks: list[dict[str, Any]] = []
    used = 0
    for symbol in matches:
        code = _read_symbol_code(repo_doc, symbol, snapshots=snapshots, env=env)
        tokens = max(1, len(str(code.get("source") or "")) // 4)
        if used + tokens > budget and chunks:
            break
        chunks.append(code)
        used += tokens
    return {"repo": repo_doc["repo"], "approx_tokens": used, "symbols": chunks}


def _exec_filtered(args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run a safe argv command without shell=True; index >5KB stdout into FTS5 when intent is set."""
    import shlex
    import subprocess
    from runtime import context_sandbox
    from runtime.config import no_window_kwargs, record_savings

    raw_argv = args.get("argv")
    if isinstance(raw_argv, list) and raw_argv:
        if not all(isinstance(item, str) for item in raw_argv):
            return {"error": "Each argv entry must be a string."}
        argv = raw_argv
    elif isinstance(args.get("command"), str) and args["command"].strip():
        try:
            argv = shlex.split(args["command"])
        except ValueError as exc:
            return {"error": f"Invalid command syntax: {exc}"}
    else:
        return {"error": "Provide `argv` (list of strings) or `command`."}

    if not argv or not argv[0]:
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
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **no_window_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "error": f"Command timed out after {timeout}s", "argv": argv,
            "stdout": (exc.stdout or b"").decode("utf-8", errors="replace"),
            "stderr": (exc.stderr or b"").decode("utf-8", errors="replace"),
        }
    except OSError as exc:
        return {"error": f"Failed to execute {argv[0]}: {exc}"}

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    cmd_label = " ".join(argv)[:160]

    raw_bytes = len(stdout.encode("utf-8"))
    # Preserve failure diagnostics without filtering or truncating either stream.
    if proc.returncode != 0:
        result = {"returncode": proc.returncode, "filtered": False, "raw_bytes": raw_bytes,
                  "stdout": stdout, "stderr": stderr}
        if raw_bytes + len(stderr.encode("utf-8")) > 8192:
            archive = context_sandbox.store_output(stdout + "\n" + stderr, command=cmd_label, env=env)
            if archive:
                result["archive_id"] = archive
        return result

    # Intent-driven filtering when stdout > 5 KB
    if raw_bytes > 5120 and intent:
        aid = context_sandbox.store_output(stdout, command=cmd_label, env=env, require_exact=True)
        matched = context_sandbox.query_output(aid, intent, limit=limit, env=env, require_hit=True) if aid else None
        if matched and len(matched) < len(stdout):
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
                "raw_bytes": raw_bytes,
                "output": matched,
                "stderr": stderr,
            }

    return {
        "returncode": 0,
        "filtered": False,
        "raw_bytes": raw_bytes,
        "output": stdout,
        "stderr": stderr,
    }


_BATCH_MAX_COMMANDS = 8


def _exec_batch(args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run up to 8 commands through ``_exec_filtered`` with one shared intent.

    Each command's output over 5 KB is archived and replaced by its lines matching the
    queries; failures keep their full output. Returns one compact text block.
    """
    commands = args.get("commands")
    if not isinstance(commands, list) or not commands:
        return {"error": "Provide `commands`: a list of command strings or argv lists."}
    if len(commands) > _BATCH_MAX_COMMANDS:
        return {"error": f"At most {_BATCH_MAX_COMMANDS} commands per batch."}
    queries = args.get("queries") or []
    if isinstance(queries, str):
        queries = [queries]
    intent = " ".join(str(q) for q in queries if str(q).strip())
    blocks: list[str] = []
    for cmd in commands:
        sub = {"cwd": args.get("cwd"), "timeout": args.get("timeout"), "limit": args.get("limit"), "intent": intent}
        if isinstance(cmd, list):
            sub["argv"] = cmd
            label = " ".join(str(c) for c in cmd)
        else:
            sub["command"] = str(cmd)
            label = str(cmd)
        res = _exec_filtered(sub, env=env)
        if "error" in res:
            blocks.append(f"## {label}\nerror: {res['error']}")
            continue
        head = f"## {label} (exit {res.get('returncode', 0)}"
        head += f", {res.get('raw_bytes', 0)} B, filtered" if res.get("filtered") else ""
        head += f", archive {res['archive_id']}" if res.get("archive_id") else ""
        body = str(res.get("output", res.get("stdout", "")) or "").rstrip()
        err = str(res.get("stderr") or "").rstrip()
        stderr_block = f"--- stderr ---\n{err}" if err else ""
        blocks.append("\n".join(x for x in (head + ")", body, stderr_block) if x))
    return {"batch": "\n\n".join(blocks)}


# ---------------------------------------------------------------------------
# Graph actions: references, importers, call hierarchy, safety verdicts,
# changed symbols, dead code, and import cycles. All are computed from the
# stored index (symbols + import tokens) plus a fresh read of the files.
# ---------------------------------------------------------------------------

_GRAPH_ACTIONS = frozenset({
    "find_references", "find_importers", "get_call_hierarchy",
    "check_edit_safe", "check_delete_safe", "check_rename_safe",
    "get_changed_symbols", "find_dead_code", "get_dependency_cycles",
})
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_CALL_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_GIT_REF_RE = re.compile(r"^[\w./~^@{}-]+$")
_GENERIC_STEMS = frozenset({"__init__", "index", "mod", "main", "lib"})
_DEAD_SKIP_NAMES = frozenset({"main", "setUp", "tearDown", "setUpClass", "tearDownClass", "setUpModule", "tearDownModule"})
_LANG_FAMILY = {
    ".py": "py", ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js", ".cjs": "js",
    ".c": "c", ".h": "c", ".cpp": "c", ".cc": "c", ".cxx": "c", ".hpp": "c", ".kt": "kt", ".kts": "kt",
}
_SAFE_CAUTION_FILES = 5


def _simple_name(name: str) -> str:
    """`Foo.bar` -> `bar`, `a::b` -> `b`, `Vec<T>` -> `Vec`."""
    base = str(name or "").split("<", 1)[0]
    parts = _IDENT_RE.findall(base)
    return parts[-1] if parts else ""


def _word_re(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")


def _is_test_path(rel: str) -> bool:
    low = rel.lower()
    parts = low.split("/")
    name = parts[-1]
    return (
        any(p in ("test", "tests", "__tests__", "spec") for p in parts[:-1])
        or name.startswith("test_")
        or Path(name).stem.endswith(("_test", "_spec"))
        or ".test." in name
        or ".spec." in name
    )


class _GraphCtx:
    """Lazy file reader + index views shared by one graph request."""

    def __init__(self, doc: dict[str, Any], env: dict[str, str] | None) -> None:
        self.doc = doc
        self.env = env
        self.slug = str(doc.get("repo") or "")
        self.root = Path(str(doc.get("source_root") or ""))
        self.files: list[str] = [str(f) for f in doc.get("source_files", [])]
        self.symbols: list[dict[str, Any]] = list(doc.get("symbols", []))
        self._texts: dict[str, str | None] = {}
        self._imports: dict[str, set[str]] | None = None

    def text(self, rel: str) -> str | None:
        if rel not in self._texts:
            try:
                self._texts[rel] = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._texts[rel] = None
        return self._texts[rel]

    def candidates(self, needle: str) -> list[str]:
        return _text_candidates(self.slug, self.root, self.files, needle, env=self.env)

    def defs_named(self, name: str) -> list[dict[str, Any]]:
        return [s for s in self.symbols if _simple_name(s.get("name", "")) == name]

    def imports(self) -> dict[str, set[str]]:
        """Import tokens per file, from the SQLite `imports` table or re-extracted."""
        if self._imports is not None:
            return self._imports
        out: dict[str, set[str]] = {}
        dbp = _db_path(self.slug, self.env)
        loaded = False
        if dbp.is_file():
            try:
                with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
                    for src, tok in conn.execute("SELECT src_file, token FROM imports").fetchall():
                        out.setdefault(str(src), set()).add(str(tok))
                loaded = True
            except sqlite3.Error:
                out = {}
        if not loaded:
            for rel in self.files:
                text = self.text(rel)
                if text is not None:
                    out[rel] = _extract_imports(rel, text)
        self._imports = out
        return out


def _graph_ctx(args: dict[str, Any], env: dict[str, str] | None) -> _GraphCtx | dict[str, Any]:
    repo_name = str(args.get("repo") or "").strip()
    doc = _load_repo(repo_name, env=env)
    if doc is None:
        return {"error": f"Repository `{repo_name}` not indexed. Call `index_folder` first."}
    ctx = _GraphCtx(doc, env)
    prefix = str(args.get("path_prefix") or "").strip().strip("/")
    if prefix:
        # Monorepo scoping: restrict every graph/analysis view to one subtree.
        ctx.files = [f for f in ctx.files if f == prefix or f.startswith(prefix + "/")]
        ctx.symbols = [s for s in ctx.symbols if str(s.get("file", "")).startswith(prefix + "/")]
    return ctx


def _references(ctx: _GraphCtx, name: str) -> list[dict[str, Any]]:
    """Every line where `name` appears as a whole word, minus its own definition lines."""
    def_lines = {(s.get("file"), int(s.get("start_line") or 0)) for s in ctx.defs_named(name)}
    pat = _word_re(name)
    out: list[dict[str, Any]] = []
    for rel in ctx.candidates(name):
        text = ctx.text(rel)
        if not text or name not in text:
            continue
        lines = [
            idx for idx, ln in enumerate(text.splitlines(), start=1)
            if (rel, idx) not in def_lines and pat.search(ln)
        ]
        if lines:
            out.append({"file": rel, "lines": lines})
    out.sort(key=lambda r: (-len(r["lines"]), r["file"]))
    return out


def _action_find_references(ctx: _GraphCtx, args: dict[str, Any]) -> dict[str, Any]:
    name = _simple_name(str(args.get("symbol") or args.get("name") or args.get("query") or ""))
    if not name:
        return {"error": "Provide `symbol` name."}
    defs = [s.get("id") for s in ctx.defs_named(name)]
    if args.get("precise") is not False:
        try:
            from runtime import index_analysis

            precise = index_analysis.scip_references(ctx, name)
        except Exception:
            precise = None
        if precise is not None:
            return {"repo": ctx.slug, "symbol": name, "definitions": defs, "references": precise, "source": "scip"}
    refs = _references(ctx, name)
    return {"repo": ctx.slug, "symbol": name, "definitions": defs, "references": refs}


def _module_parts(module: str) -> list[str]:
    raw = str(module or "").strip().replace("\\", "/")
    if Path(raw).suffix.lower() in _CODE_EXTS:
        raw = raw[: -len(Path(raw).suffix)]
    parts = [p for p in re.split(r"[/.:]+", raw) if p and p not in (".", "..", "@")]
    if len(parts) > 1 and parts[-1] in _GENERIC_STEMS:
        parts = parts[:-1]
    return parts


def _action_find_importers(ctx: _GraphCtx, args: dict[str, Any]) -> dict[str, Any]:
    module = str(args.get("module") or args.get("file_path") or args.get("symbol") or "").strip()
    parts = _module_parts(module)
    if not parts:
        return {"error": "Provide `module` (dotted name or repo-relative path)."}
    last = parts[-1]
    tail = "/".join(parts)
    targets = []
    for rel in ctx.files:
        stem_path = rel[: -len(Path(rel).suffix)] if Path(rel).suffix else rel
        if stem_path.endswith("/" + Path(stem_path).name) and Path(stem_path).name in _GENERIC_STEMS:
            stem_path = stem_path.rsplit("/", 1)[0]
        if stem_path == tail or stem_path.endswith("/" + tail):
            targets.append(rel)
    target_dirs = {str(Path(t).parent).replace("\\", "/") for t in targets}
    importers: list[dict[str, Any]] = []
    for rel, toks in sorted(ctx.imports().items()):
        if rel in targets or last not in toks:
            continue
        if all(p in toks for p in parts):
            match = "exact"
        elif str(Path(rel).parent).replace("\\", "/") in target_dirs:
            match = "relative"
        else:
            match = "name"
        importers.append({"file": rel, "match": match})
    rank = {"exact": 0, "relative": 1, "name": 2}
    importers.sort(key=lambda x: (rank[x["match"]], x["file"]))
    return {"repo": ctx.slug, "module": module, "targets": targets, "importers": importers}


def _call_graph(ctx: _GraphCtx) -> dict[str, set[str]]:
    """Map symbol id -> names it calls (names limited to indexed symbols)."""
    known = {_simple_name(s.get("name", "")) for s in ctx.symbols} - {""}
    by_file: dict[str, list[dict[str, Any]]] = {}
    for s in ctx.symbols:
        by_file.setdefault(str(s.get("file")), []).append(s)
    calls: dict[str, set[str]] = {}
    for rel, fsyms in by_file.items():
        text = ctx.text(rel)
        if not text:
            continue
        lines = text.splitlines()
        owner: list[dict[str, Any] | None] = [None] * (len(lines) + 2)
        # Largest spans first so nested symbols overwrite their containers.
        for s in sorted(fsyms, key=lambda x: -(int(x.get("end_line") or 0) - int(x.get("start_line") or 0))):
            lo = max(1, int(s.get("start_line") or 1))
            hi = min(len(lines), int(s.get("end_line") or lo))
            for i in range(lo, hi + 1):
                owner[i] = s
        def_starts = {(int(s.get("start_line") or 0), _simple_name(s.get("name", ""))) for s in fsyms}
        for idx, ln in enumerate(lines, start=1):
            sym = owner[idx]
            if sym is None or "(" not in ln:
                continue
            for m in _CALL_RE.finditer(ln):
                name = m.group(1)
                if name in known and (idx, name) not in def_starts:
                    calls.setdefault(str(sym.get("id")), set()).add(name)
    return calls


def _action_get_call_hierarchy(ctx: _GraphCtx, args: dict[str, Any]) -> dict[str, Any]:
    raw = str(args.get("symbol") or args.get("symbol_id") or "").strip()
    if not raw:
        return {"error": "Provide `symbol` (name or symbol id)."}
    try:
        depth = max(1, min(4, int(args.get("depth") or 2)))
    except (TypeError, ValueError):
        return {"error": "`depth` must be an integer."}
    roots = [s for s in ctx.symbols if s.get("id") == raw or s.get("qualified_name") == raw]
    if not roots:
        roots = ctx.defs_named(_simple_name(raw))
    if not roots:
        return {"error": f"Symbol `{raw}` not found in `{ctx.slug}`."}
    calls = _call_graph(ctx)
    by_id = {str(s.get("id")): s for s in ctx.symbols}
    by_name: dict[str, list[str]] = {}
    for s in ctx.symbols:
        by_name.setdefault(_simple_name(s.get("name", "")), []).append(str(s.get("id")))
    callers_of: dict[str, list[str]] = {}
    for sid, names in calls.items():
        for n in names:
            callers_of.setdefault(n, []).append(sid)
    budget = [80]
    imports = ctx.imports()

    def file_key(rel: str) -> str:
        fp = Path(rel)
        return fp.parent.name if fp.stem in _GENERIC_STEMS and fp.parent.name else fp.stem

    def linked(caller_id: str, callee_id: str) -> bool:
        """A name-based edge is kept when the name is unique, or the files are the same or import-linked."""
        callee = by_id.get(callee_id, {})
        if len(by_name.get(_simple_name(callee.get("name", "")), [])) <= 1:
            return True
        c_file = str(by_id.get(caller_id, {}).get("file"))
        d_file = str(callee.get("file"))
        return c_file == d_file or file_key(d_file) in imports.get(c_file, set())

    def node(sid: str, children: list[dict[str, Any]]) -> dict[str, Any]:
        s = by_id.get(sid, {})
        return {"id": sid, "line": s.get("start_line"), "children": children}

    def walk_up(sid: str, level: int, seen: set[str]) -> list[dict[str, Any]]:
        if level > depth:
            return []
        out = []
        for caller in sorted(set(callers_of.get(_simple_name(by_id.get(sid, {}).get("name", "")), []))):
            if caller in seen or budget[0] <= 0 or not linked(caller, sid):
                continue
            budget[0] -= 1
            out.append(node(caller, walk_up(caller, level + 1, seen | {caller})))
        return out

    def walk_down(sid: str, level: int, seen: set[str]) -> list[dict[str, Any]]:
        if level > depth:
            return []
        out = []
        for name in sorted(calls.get(sid, ())):
            cands = by_name.get(name, [])
            plausible = [c for c in cands if linked(sid, c)] or cands
            for callee in plausible:
                if callee in seen or budget[0] <= 0:
                    continue
                budget[0] -= 1
                out.append(node(callee, walk_down(callee, level + 1, seen | {callee})))
        return out

    root = roots[0]
    rid = str(root.get("id"))
    return {
        "repo": ctx.slug,
        "symbol": rid,
        "also": [str(s.get("id")) for s in roots[1:]],
        "depth": depth,
        "callers": walk_up(rid, 1, {rid}),
        "callees": walk_down(rid, 1, {rid}),
        "truncated": budget[0] <= 0,
    }


def _action_check_safe(ctx: _GraphCtx, action: str, args: dict[str, Any]) -> dict[str, Any]:
    name = _simple_name(str(args.get("symbol") or args.get("name") or ""))
    if not name:
        return {"error": "Provide `symbol` name."}
    blast = dispatch_action("get_blast_radius", {"repo": ctx.slug, "symbol": name}, env=ctx.env)
    impacted = {str(i["file"]): i for i in blast.get("impacted_files", []) if isinstance(i, dict)}
    refs = _references(ctx, name)
    defs = ctx.defs_named(name)
    def_files = {str(s.get("file")) for s in defs}
    ref_files = {r["file"]: len(r["lines"]) for r in refs}
    affected: list[dict[str, Any]] = []
    for rel in sorted(set(impacted) | set(ref_files) | def_files):
        item = impacted.get(rel, {})
        affected.append({
            "file": rel,
            "refs": ref_files.get(rel, 0),
            "defines": rel in def_files,
            "importer": bool(item.get("importer")),
        })
    affected.sort(key=lambda a: (not a["defines"], -a["refs"], a["file"]))
    external = [a for a in affected if not a["defines"] and a["refs"]]
    total_refs = sum(ref_files.values())
    result: dict[str, Any] = {"repo": ctx.slug, "symbol": name, "definitions": [s.get("id") for s in defs]}
    if not defs:
        result.update(verdict="UNKNOWN", reason=f"`{name}` is not an indexed symbol; {total_refs} textual refs.")
    elif action == "check_edit_safe":
        if not external:
            result.update(verdict="SAFE", reason="No references outside the defining file(s).")
        elif len(external) <= _SAFE_CAUTION_FILES:
            result.update(verdict="CAUTION", reason=f"Signature changes must update {len(external)} other file(s).")
        else:
            result.update(verdict="RISKY", reason=f"{len(external)} other files reference it; keep the signature stable.")
    elif action == "check_delete_safe":
        if total_refs == 0:
            result.update(verdict="SAFE", reason="No references anywhere besides the definition.")
        else:
            result.update(verdict="UNSAFE", reason=f"{total_refs} reference(s) in {len(ref_files)} file(s) would break.")
    else:
        new_name = _simple_name(str(args.get("new_name") or ""))
        result["new_name"] = new_name
        clash = [s.get("id") for s in ctx.defs_named(new_name)] if new_name else []
        if clash:
            result.update(verdict="CONFLICT", reason=f"`{new_name}` is already defined: {', '.join(map(str, clash[:5]))}.")
        elif not external:
            result.update(verdict="SAFE", reason="Only the defining file(s) need the rename.")
        else:
            result.update(verdict="REVIEW", reason=f"Rename touches {len(external) + len(def_files)} file(s), {total_refs} reference(s).")
    result["affected_files"] = affected[:40]
    verdict = str(result.get("verdict") or "")
    terminal = verdict in ("SAFE", "UNSAFE", "CONFLICT")
    result["stop_rule"] = {
        "terminal": terminal,
        "reason": (
            f"Verdict {verdict} is definitive for the indexed corpus; no extra verification query needed."
            if terminal
            else f"Verdict {verdict} requires inspecting affected files or refreshing the index."
        ),
    }
    return result


def _git(root: Path, argv: list[str]) -> str | None:
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *argv],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _action_get_changed_symbols(ctx: _GraphCtx, args: dict[str, Any]) -> dict[str, Any]:
    ref = str(args.get("ref") or args.get("base") or "").strip()
    if ref and (ref.startswith("-") or not _GIT_REF_RE.match(ref)):
        return {"error": f"Invalid git ref `{ref}`."}
    argv = ["diff", "--no-color", "--no-ext-diff", "--relative", "-U0"]
    if args.get("staged") is True:
        argv.append("--cached")
    if ref:
        argv.append(ref)
    out = _git(ctx.root, argv)
    if out is None:
        return {"error": "`git diff` failed (not a git repo, or bad ref)."}
    ranges: dict[str, list[tuple[int, int]]] = {}
    deleted: list[str] = []
    old_path = ""
    cur: str | None = None
    for line in out.splitlines():
        if line.startswith("--- "):
            old_path = line[6:] if line.startswith("--- a/") else ""
        elif line.startswith("+++ "):
            if line.startswith("+++ b/"):
                cur = line[6:]
                ranges.setdefault(cur, [])
            else:
                cur = None
                if old_path:
                    deleted.append(old_path)
        elif cur is not None and line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                lo = max(1, start)
                ranges[cur].append((lo, lo + count - 1 if count else lo))
    added: list[str] = []
    if args.get("include_untracked", True) is not False and args.get("staged") is not True:
        untracked = _git(ctx.root, ["ls-files", "--others", "--exclude-standard"]) or ""
        for rel in untracked.splitlines():
            rel = rel.strip()
            if rel and Path(rel).suffix.lower() in _CODE_EXTS:
                added.append(rel)
                ranges[rel] = [(1, 10**9)]
    files: list[dict[str, Any]] = []
    for rel, hunks in sorted(ranges.items()):
        if Path(rel).suffix.lower() not in _CODE_EXTS or not hunks:
            continue
        syms = _index_single_file(ctx.root, ctx.root / rel)
        hit = [
            s for s in syms
            if any(int(s["start_line"]) <= hi and int(s["end_line"]) >= lo for lo, hi in hunks)
        ]
        # Report the innermost symbols: drop a container when a nested symbol also matched.
        inner = [
            s for s in hit
            if not any(
                o is not s and int(o["start_line"]) >= int(s["start_line"]) and int(o["end_line"]) <= int(s["end_line"])
                and (o["start_line"], o["end_line"]) != (s["start_line"], s["end_line"])
                for o in hit
            )
        ]
        outside = [
            (lo, hi) for lo, hi in hunks
            if not any(int(s["start_line"]) <= hi and int(s["end_line"]) >= lo for s in syms)
        ]
        files.append({
            "file": rel,
            "status": "added" if rel in added else "modified",
            "symbols": [
                {"id": s["id"], "start_line": s["start_line"], "end_line": s["end_line"],
                 "hunks": sum(1 for lo, hi in hunks if int(s["start_line"]) <= hi and int(s["end_line"]) >= lo)}
                for s in inner
            ],
            "module_level": [[lo, min(hi, 10**6)] for lo, hi in outside if rel not in added],
        })
    for rel in deleted:
        if Path(rel).suffix.lower() in _CODE_EXTS:
            files.append({
                "file": rel, "status": "deleted",
                "symbols": [{"id": s.get("id"), "start_line": s.get("start_line"), "end_line": s.get("end_line"), "hunks": 1}
                            for s in ctx.symbols if s.get("file") == rel],
                "module_level": [],
            })
    return {"repo": ctx.slug, "ref": ref or "worktree", "files": files}


def _is_marked(ctx: _GraphCtx, sym: dict[str, Any]) -> bool:
    """True when a decorator/attribute (`@x`, `#[test]`) sits above the symbol: likely registered dynamically."""
    text = ctx.text(str(sym.get("file") or "")) or ""
    lines = text.splitlines()
    i = int(sym.get("start_line") or 1) - 2
    while 0 <= i < len(lines) and not lines[i].strip():
        i -= 1
    if not (0 <= i < len(lines)):
        return False
    prev = lines[i].strip()
    return prev.startswith("@") or prev.startswith("#[")


def _action_find_dead_code(ctx: _GraphCtx, args: dict[str, Any]) -> dict[str, Any]:
    from collections import Counter

    include_tests = args.get("include_tests") is True
    limit = max(1, min(200, int(args.get("limit") or 50)))
    counts: Counter[str] = Counter()
    for rel in ctx.files:
        text = ctx.text(rel)
        if text:
            counts.update(_IDENT_RE.findall(text))
    def_counts: Counter[str] = Counter(_simple_name(s.get("name", "")) for s in ctx.symbols)
    dead: list[dict[str, Any]] = []
    for s in ctx.symbols:
        name = _simple_name(s.get("name", ""))
        rel = str(s.get("file") or "")
        if (
            len(name) < 2
            or (name.startswith("__") and name.endswith("__"))
            or name in _DEAD_SKIP_NAMES
            or name.lower().startswith("test")
            or s.get("kind") in ("impl", "module")
            or (not include_tests and _is_test_path(rel))
        ):
            continue
        if counts[name] <= def_counts[name] and not _is_marked(ctx, s):
            dead.append({"id": s.get("id"), "kind": s.get("kind"), "start_line": s.get("start_line"), "end_line": s.get("end_line")})
    dead.sort(key=lambda d: (str(d["id"]),))
    return {"repo": ctx.slug, "candidates": dead[:limit], "total": len(dead)}


def _file_graph(ctx: _GraphCtx) -> dict[str, set[str]]:
    """Resolve import tokens to indexed files (same language family, ambiguity dropped)."""
    keys: dict[str, list[str]] = {}
    for rel in ctx.files:
        p = Path(rel)
        key = p.parent.name if p.stem in _GENERIC_STEMS and p.parent.name else p.stem
        keys.setdefault(key, []).append(rel)
    graph: dict[str, set[str]] = {}
    for src, toks in ctx.imports().items():
        fam = _LANG_FAMILY.get(Path(src).suffix.lower(), Path(src).suffix.lower())
        src_dir = str(Path(src).parent)
        for tok in toks:
            cands = [
                c for c in keys.get(tok, [])
                if c != src and _LANG_FAMILY.get(Path(c).suffix.lower(), Path(c).suffix.lower()) == fam
            ]
            if len(cands) > 1:
                narrowed = [
                    c for c in cands
                    if str(Path(c).parent) == src_dir
                    or (Path(c).stem not in _GENERIC_STEMS and Path(c).parent.name in toks)
                ]
                cands = narrowed if len(narrowed) == 1 else []
            if cands:
                graph.setdefault(src, set()).add(cands[0])
    return graph


def _strongly_connected(graph: dict[str, set[str]]) -> list[list[str]]:
    """Iterative Tarjan; returns components with more than one node."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    comps: list[list[str]] = []
    counter = 0
    nodes = sorted(set(graph) | {d for ds in graph.values() for d in ds})
    for start in nodes:
        if start in index:
            continue
        work = [(start, iter(sorted(graph.get(start, ()))))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(graph.get(w, ())))))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    comps.append(sorted(comp))
    return comps


def _cycle_path(graph: dict[str, set[str]], comp: list[str]) -> list[str]:
    """Shortest cycle through the component's first node (BFS inside the component)."""
    members = set(comp)
    start = comp[0]
    prev: dict[str, str] = {}
    queue = [start]
    seen = {start}
    while queue:
        v = queue.pop(0)
        for w in sorted(graph.get(v, ())):
            if w not in members:
                continue
            if w == start:
                path = [v]
                while path[-1] != start:
                    path.append(prev[path[-1]])
                return list(reversed(path)) + [start]
            if w not in seen:
                seen.add(w)
                prev[w] = v
                queue.append(w)
    return comp + [start]


def _action_get_dependency_cycles(ctx: _GraphCtx, args: dict[str, Any]) -> dict[str, Any]:
    graph = _file_graph(ctx)
    comps = _strongly_connected(graph)
    comps.sort(key=lambda c: (-len(c), c[0]))
    cycles = [{"files": len(c), "cycle": _cycle_path(graph, c), "members": c[:30]} for c in comps[:20]]
    return {"repo": ctx.slug, "edges": sum(len(v) for v in graph.values()), "cycles": cycles}


def _graph_action(action: str, args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    ctx = _graph_ctx(args, env)
    if isinstance(ctx, dict):
        return ctx
    if action == "find_references":
        return _action_find_references(ctx, args)
    if action == "find_importers":
        return _action_find_importers(ctx, args)
    if action == "get_call_hierarchy":
        return _action_get_call_hierarchy(ctx, args)
    if action in ("check_edit_safe", "check_delete_safe", "check_rename_safe"):
        return _action_check_safe(ctx, action, args)
    if action == "get_changed_symbols":
        return _action_get_changed_symbols(ctx, args)
    if action == "find_dead_code":
        return _action_find_dead_code(ctx, args)
    return _action_get_dependency_cycles(ctx, args)


def _fmt_lines(lines: list[int], cap: int = 20) -> str:
    shown = ",".join(str(n) for n in lines[:cap])
    return shown + (f",+{len(lines) - cap}" if len(lines) > cap else "")


def _fmt_tree(nodes: list[dict[str, Any]], indent: int = 1) -> list[str]:
    out: list[str] = []
    for n in nodes:
        out.append(f"{'  ' * indent}{n.get('id')} L{n.get('line')}")
        out.extend(_fmt_tree(n.get("children") or [], indent + 1))
    return out


def format_graph_response(action: str, p: dict[str, Any]) -> str | None:
    """Compact plain-text rendering for graph actions and `purge_sandbox`."""
    repo = p.get("repo", "")
    if action == "find_references" and isinstance(p.get("references"), list):
        refs = p["references"]
        total = sum(len(r["lines"]) for r in refs)
        defs = ", ".join(str(d) for d in p.get("definitions", [])[:5]) or "none indexed"
        lines = [f"repo={repo} symbol={p.get('symbol')} ({total} refs in {len(refs)} files; def: {defs}):"]
        lines.extend(f"  {r['file']}: {_fmt_lines(r['lines'])}" for r in refs[:60])
        if len(refs) > 60:
            lines.append(f"  ... +{len(refs) - 60} files")
        return "\n".join(lines)
    if action == "find_importers" and isinstance(p.get("importers"), list):
        imp = p["importers"]
        tgt = ", ".join(p.get("targets", [])[:5]) or "no indexed file"
        lines = [f"repo={repo} module={p.get('module')} -> {tgt} ({len(imp)} importers):"]
        lines.extend(f"  {i['file']}" + ("" if i["match"] == "exact" else f" [{i['match']}]") for i in imp[:80])
        return "\n".join(lines)
    if action == "get_call_hierarchy" and "callers" in p:
        lines = [f"repo={repo} symbol={p.get('symbol')} depth={p.get('depth')}" + (" (truncated)" if p.get("truncated") else "")]
        if p.get("also"):
            lines.append(f"also named: {', '.join(p['also'][:5])}")
        lines.append(f"callers ({len(p['callers'])}):" if p["callers"] else "callers: none")
        lines.extend(_fmt_tree(p["callers"]))
        lines.append(f"callees ({len(p['callees'])}):" if p["callees"] else "callees: none")
        lines.extend(_fmt_tree(p["callees"]))
        return "\n".join(lines)
    if action in ("check_edit_safe", "check_delete_safe", "check_rename_safe") and "verdict" in p:
        target = p.get("symbol", "")
        if p.get("new_name"):
            target += f" -> {p['new_name']}"
        lines = [f"{p['verdict']}: {action[6:-5]} {target} — {p.get('reason', '')}"]
        for a in p.get("affected_files", []):
            tags = [t for t, on in (("def", a["defines"]), ("imp", a["importer"])) if on]
            lines.append(f"  {a['file']} ({a['refs']} refs)" + (f" [{','.join(tags)}]" if tags else ""))
        return "\n".join(lines)
    if action == "get_changed_symbols" and isinstance(p.get("files"), list):
        files = p["files"]
        n_syms = sum(len(f["symbols"]) for f in files)
        lines = [f"repo={repo} diff={p.get('ref')} ({len(files)} files, {n_syms} symbols):"]
        for f in files:
            lines.append(f"{f['file']}" + ("" if f["status"] == "modified" else f" [{f['status']}]") + ":")
            for s in f["symbols"]:
                extra = f" ({s['hunks']} hunks)" if s.get("hunks", 1) > 1 else ""
                lines.append(f"  {s['id']} L{s['start_line']}-{s['end_line']}{extra}")
            for lo, hi in f.get("module_level", []):
                lines.append(f"  module-level L{lo}" + (f"-{hi}" if hi != lo else ""))
        return "\n".join(lines)
    if action == "find_dead_code" and isinstance(p.get("candidates"), list):
        c = p["candidates"]
        lines = [f"repo={repo} ({p.get('total', len(c))} unreferenced symbols; name-based, dynamic use not seen):"]
        lines.extend(f"  {d['id']} L{d['start_line']}-{d['end_line']} {d['kind']}" for d in c)
        if p.get("total", 0) > len(c):
            lines.append(f"  ... +{p['total'] - len(c)} more (raise `limit`)")
        return "\n".join(lines)
    if action == "get_dependency_cycles" and isinstance(p.get("cycles"), list):
        cyc = p["cycles"]
        if not cyc:
            return f"repo={repo} no import cycles ({p.get('edges', 0)} resolved edges)."
        lines = [f"repo={repo} {len(cyc)} import cycle(s) ({p.get('edges', 0)} resolved edges):"]
        for c in cyc:
            lines.append(f"  [{c['files']} files] " + " -> ".join(c["cycle"]))
        return "\n".join(lines)
    if action == "purge_sandbox" and "deleted" in p:
        ids = ", ".join(p.get("ids", [])[:10])
        return f"Purged {p['deleted']} archive(s), {p.get('raw_deleted', 0)} raw sidecar(s)" + (f": {ids}" if ids else ".")
    return None


def dispatch_action(action: str, args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    if action in _GRAPH_ACTIONS:
        return _graph_action(action, args, env=env)
    if action in _CODE_MODE_ACTIONS:
        from runtime import code_mode

        return code_mode.dispatch(action, args, env=env) or {"error": f"Unknown action `{action}`."}
    if action in _ANALYSIS_ACTIONS:
        from runtime import index_analysis

        return index_analysis.dispatch(action, args, env=env)

    if action == "purge_sandbox":
        from runtime import context_sandbox

        aid = str(args.get("archive_id") or "").strip() or None
        sess = str(args.get("session") or args.get("session_id") or "").strip() or None
        return context_sandbox.purge_archives(
            archive_id=aid, session_id=sess, everything=args.get("all") is True, env=env
        )

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

    if action == "fetch_and_index":
        from runtime import web_fetch

        return web_fetch.fetch_and_index(args, env=env)

    if action == "execute_batch":
        return _exec_batch(args, env=env)

    if action == "index_folder":
        return index_folder(str(args.get("path") or os.getcwd()), env=env)

    if action == "list_repos":
        idir = index_dir(env)
        repos = []
        seen_slugs: set[str] = set()
        if idir.is_dir():
            for p in sorted(idir.glob("*.meta.json")):
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    slug = str(d.get("repo") or p.name[:-10])
                    seen_slugs.add(slug)
                    repos.append({
                        "repo": slug,
                        "source_root": d.get("source_root", ""),
                        "files": d.get("file_count", 0),
                        "symbols": d.get("symbol_count", 0),
                    })
                except Exception:
                    continue
            for p in sorted(idir.glob("*.json")):
                if p.name.endswith(".meta.json") or p.stem in seen_slugs:
                    continue
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

    repo_name = str(args.get("repo") or "").strip()
    source_range = {}
    if action == "get_symbol_source":
        source_range = {key: args[key] for key in ("start_line", "end_line") if args.get(key) is not None}
        if any(type(n) is not int or n < 1 for n in source_range.values()):
            return {"error": "start_line and end_line must be positive integers."}
        if source_range.get("end_line", float("inf")) < source_range.get("start_line", 1):
            return {"error": "end_line must be at least start_line."}
    if action == "invalidate_cache" and not repo_name:
        return {"error": "Provide explicit `repo` name to invalidate."}
    # Fast SQLite-first paths (avoid loading entire repo into memory for targeted queries)
    resolved = _resolve_repo_slug(repo_name, env=env)
    if resolved is not None:
        r_slug, r_meta = resolved
        if action == "invalidate_cache":
            for ext in (".meta.json", ".json", ".db", ".db-wal", ".db-shm"):
                try:
                    (index_dir(env) / f"{r_slug}{ext}").unlink(missing_ok=True)
                except OSError:
                    pass
            return {"invalidated": r_slug}
        if action == "get_file_tree":
            db_files = _db_query_files(r_slug, env=env)
            if db_files is not None:
                return {"repo": r_slug, "files": db_files}
        if action == "get_file_outline":
            fp = str(args.get("file_path") or "").replace("\\", "/")
            db_syms = _db_query_file_outline(r_slug, fp, env=env)
            if db_syms is not None:
                return {"file": fp, "symbols": db_syms}
        if action == "search_symbols":
            q = str(args.get("query") or "").strip().lower()
            terms = [t for t in re.split(r"\s+", q) if t]
            limit = int(args.get("limit") or 20)
            db_matches = _db_query_search_symbols(r_slug, terms, limit, env=env)
            if db_matches is not None:
                return {"repo": r_slug, "matches": db_matches}
        if action == "get_symbol_source":
            sid = str(args.get("symbol_id") or "")
            db_sym = _db_query_symbol_by_id(r_slug, sid, env=env)
            if db_sym is not None:
                return _read_symbol_code(r_meta, db_sym, **source_range, env=env)
            if _db_path(r_slug, env).is_file():
                return {"error": f"Symbol `{sid}` not found in `{r_slug}`."}
        if action == "get_symbols":
            ids = [str(x) for x in (args.get("symbol_ids") or []) if x]
            db_syms = _db_query_symbols_by_ids(r_slug, ids, env=env)
            if db_syms is not None:
                snapshots: dict[Path, bytes] = {}
                return {"symbols": [_read_symbol_code(r_meta, s, snapshots=snapshots, env=env) for s in db_syms]}
        if action == "get_repo_outline":
            db_outline = _db_query_repo_outline(r_slug, env=env)
            if db_outline is not None:
                return {
                    "repo": r_slug,
                    "files": r_meta.get("file_count", 0),
                    "symbols": r_meta.get("symbol_count", 0),
                    "outline": db_outline,
                }
        if action == "assemble_task_context":
            task = str(args.get("task") or "").strip().lower()
            terms = [t for t in re.split(r"\s+", task) if t]
            budget = int(args.get("token_budget") or 4000)
            db_matches = _db_query_search_symbols(r_slug, terms, 12, env=env)
            if db_matches is not None:
                return _assemble_sources(r_meta, db_matches, budget, env=env)
        if action == "get_blast_radius":
            sym_name = str(args.get("symbol") or args.get("query") or "").strip()
            if not sym_name:
                return {"error": "Provide `symbol` name."}
            db_files = _db_query_files(r_slug, env=env)
            db_defs = _db_query_def_files(r_slug, sym_name, env=env)
            if db_files is not None and db_defs is not None:
                pat = re.compile(rf"\b{re.escape(sym_name)}\b")
                call_pat = re.compile(rf"(?:\.|::|\b){re.escape(sym_name)}\s*\(")
                root = Path(r_meta.get("source_root", ""))
                imp_tokens: set[str] = {sym_name}
                for df in db_defs:
                    stem = Path(df).stem
                    if stem not in ("__init__", "index", "mod", "main"):
                        imp_tokens.add(stem)
                importers = _query_db_importers(r_slug, imp_tokens, env=env)
                has_graph = bool(importers)
                impacted: list[dict[str, Any]] = []
                for rel in _text_candidates(r_slug, root, list(db_files), sym_name, env=env):
                    is_def = rel in db_defs
                    is_imp = rel in importers
                    try:
                        text = (root / rel).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    count = len(pat.findall(text))
                    if not count:
                        continue
                    if has_graph and not is_def and not is_imp and not call_pat.search(text):
                        continue
                    entry: dict[str, Any] = {"file": rel, "references": count}
                    if is_def:
                        entry["defines"] = True
                    if is_imp:
                        entry["importer"] = True
                    impacted.append(entry)
                impacted.sort(key=lambda x: (not x.get("defines", False), not x.get("importer", False), -x["references"]))
                return {"repo": r_slug, "symbol": sym_name, "impacted_files": impacted[:30]}

    doc = _load_repo(repo_name, env=env)
    if doc is None:
        return {"error": f"Repository `{repo_name}` not indexed. Call `index_folder` first."}

    if action == "invalidate_cache":
        for ext in (".meta.json", ".json", ".db", ".db-wal", ".db-shm"):
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
                return _read_symbol_code(doc, s, **source_range, env=env)
        return {"error": f"Symbol `{sid}` not found in `{doc['repo']}`."}

    if action == "get_symbols":
        ids = set(args.get("symbol_ids") or [])
        snapshots = {}
        found = [_read_symbol_code(doc, s, snapshots=snapshots, env=env) for s in doc.get("symbols", []) if s.get("id") in ids]
        return {"symbols": found}

    if action == "search_text":
        q = str(args.get("query") or "")
        if not q:
            return {"matches": []}
        root = Path(doc["source_root"])
        low_q = q.lower()
        hits: list[dict[str, Any]] = []
        for rel in _text_candidates(doc["repo"], root, list(doc.get("source_files", [])), q, env=env):
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
        for rel in _text_candidates(doc["repo"], root, list(doc.get("source_files", [])), sym_name, env=env):
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
        return _assemble_sources(doc, res.get("matches", []), budget, env=env)

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
        "description": "Search indexed symbols by name (repo optional if cwd or sole repo is indexed).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_symbol_source",
        "description": "Fetch exact symbol source, optionally restricted to inclusive file lines.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "symbol_id": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["symbol_id"],
        },
    },
]

_HUD_APP_URI = "ui://terser/hud.html"
_HUD_APP_MIME = "text/html;profile=mcp-app"
_HUD_APP_META = {"ui": {"prefersBorder": False}}

_HUD_CONFIG_BOOL_KEYS = frozenset({
    "enabled", "shadow", "read_cache", "delta_diff", "outline_first", "form_repair",
    "bash_compress", "cmd_delta", "fts_sandbox", "loop_guard", "mcp_refetch_guard",
    "repo_map", "index_auto_reindex",
    "subagent_model_routing", "subagent_verify",
})
_HUD_CONFIG_STR_KEYS = frozenset({
    "subagent_cheap_model",
    "subagent_cheap_effort",
    "subagent_cheap_model_antigravity",
    "subagent_cheap_effort_antigravity",
    "toolprune",
    "code_discipline",
})
_HUD_CONFIG_FLOAT_KEYS = frozenset({
    "price_input_per_mtok", "price_output_per_mtok", "cache_write_mult", "cache_read_mult",
})

_ANTIGRAVITY_APP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "terser_hud",
        "title": "Terser HUD",
        "description": "Open the live Terser HUD (token savings, API usage, costs, context fill, quality grade, router & engine controls).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "description": "Initial view: 'hud' or 'details'."},
                "cwd": {"type": "string"},
                "session_id": {"type": "string"},
            },
        },
        "_meta": {
            "ui": {"resourceUri": _HUD_APP_URI, "visibility": ["model", "app"]},
            "com.google.antigravity/ui": {
                "entrypoints": [
                    {"type": "aux_pane", "title": "Terser HUD"},
                    {"type": "global", "title": "Terser Dashboard"},
                ]
            },
        },
    },
    {
        "name": "terser_hud_snapshot",
        "title": "Refresh Terser HUD",
        "description": "Fetch a fresh Terser HUD telemetry snapshot for the UI.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "session_id": {"type": "string"},
            },
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "terser_hud_set_config",
        "title": "Update Terser HUD Setting",
        "description": "Update an allowlisted Terser setting from the HUD UI and return a fresh snapshot.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "updates": {"type": "object"},
                "cwd": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["updates"],
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
]


def _mcp_apps_enabled() -> bool:
    client = (os.environ.get("TERSER_CLIENT_ID") or "").strip().lower()
    flag = (os.environ.get("TERSER_MCP_APP") or "").strip().lower()
    return client == "antigravity" or flag in ("1", "true", "yes")


def _active_mcp_tools() -> list[dict[str, Any]]:
    return [*_MCP_TOOLS, *_ANTIGRAVITY_APP_TOOLS] if _mcp_apps_enabled() else list(_MCP_TOOLS)


def _read_hud_app_html() -> str:
    override = (os.environ.get("TERSER_HUD_APP_HTML") or "").strip()
    path = Path(os.path.expanduser(override)) if override else (_PLUGIN_ROOT / "assets" / "hud_app.html")
    return path.read_text(encoding="utf-8")


def _hud_summary_text(data: dict[str, Any]) -> str:
    def _t(n: Any) -> str:
        v = int(n or 0)
        return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.1f}k" if v >= 1e3 else str(v)

    def _u(n: Any) -> str:
        v = float(n or 0.0)
        return f"${v:.0f}" if v >= 100 else f"${v:.2f}" if v >= 1 else f"${v:.3f}"

    s = data.get("session") or {}
    lt = data.get("lifetime") or {}
    parts = [f"Terser HUD (v{data.get('version', SERVER_VERSION)}):"]
    parts.append(f"saved {_t(s.get('saved_tok'))} ({_u(s.get('saved_usd'))}) this session")
    if s.get("cost_usd") is not None:
        parts.append(f"est. cost {_u(s.get('cost_usd'))}")
    if s.get("ctx_fill") is not None:
        parts.append(f"ctx {round(float(s['ctx_fill']) * 100)}%")
    if isinstance(s.get("grade"), dict):
        parts.append(f"grade {s['grade'].get('letter', '-')}")
    parts.append(f"all-time {_t(lt.get('saved_tok'))} saved")
    return " · ".join(parts)


def _handle_hud_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from runtime import config as _cfg_mod
    from runtime import hud as _hud_mod

    cwd = str(arguments.get("cwd") or os.getcwd())
    session_id = str(arguments.get("session_id") or "")
    if name == "terser_hud_set_config":
        updates = arguments.get("updates")
        if not isinstance(updates, dict):
            raise ValueError("updates must be an object")
        cfg = _cfg_mod.load_config()
        changed: dict[str, Any] = {}
        for k, v in updates.items():
            if k in _HUD_CONFIG_BOOL_KEYS:
                if not isinstance(v, bool):
                    raise ValueError(f"{k} must be a boolean")
                cfg[k] = v
                changed[k] = v
            elif k in _HUD_CONFIG_STR_KEYS:
                if not isinstance(v, str):
                    raise ValueError(f"{k} must be a string")
                sv_val = v.strip()[:64]
                allowed = {
                    "subagent_cheap_model_antigravity": ("flash_lite", "flash", "pro"),
                    "subagent_cheap_effort_antigravity": ("", "low", "medium", "high", "max"),
                    "subagent_cheap_effort": ("", "low", "medium", "high", "xhigh", "max"),
                    "toolprune": ("propose", "auto", "off"),
                    "code_discipline": ("minimal", "off"),
                }.get(k)
                if allowed is not None and sv_val not in allowed:
                    raise ValueError(f"{k} must be one of {allowed}")
                cfg[k] = sv_val
                changed[k] = sv_val
            elif k in _HUD_CONFIG_FLOAT_KEYS:
                try:
                    fv = float(v)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{k} must be a number") from exc
                if fv < 0 or fv > 10000:
                    raise ValueError(f"{k} out of range")
                cfg[k] = fv
                changed[k] = fv
            else:
                raise ValueError(f"Unsupported HUD setting: {k}")
        if changed:
            _cfg_mod.save_config(cfg)
    _hud_mod._touch_heartbeat()
    snap = _hud_mod.snapshot(session_id=session_id, cwd=cwd, host="antigravity")
    if name == "terser_hud" and arguments.get("view"):
        snap["initial_view"] = str(arguments["view"])
    return {
        "content": [{"type": "text", "text": _hud_summary_text(snap)}],
        "structuredContent": snap,
    }


def _compact_source_batch(symbols: list[dict[str, Any]]) -> list[str]:
    """Group exact source by file revision and preserve symbol order in a range map."""
    files: dict[tuple[str, str], dict[int, str]] = {}
    mapping: list[str] = []
    for symbol in symbols:
        if not isinstance(symbol, dict):
            return []
        if symbol.get("error"):
            mapping.append(f"Error: {symbol['error']}")
            continue
        if not symbol.get("_revision") or not isinstance(symbol.get("source"), str):
            return []
        file, start, end = symbol["file"], symbol["start_line"], symbol["end_line"]
        lines = files.setdefault((file, symbol["_revision"]), {})
        source = symbol["source"].split("\n")
        if len(source) != end - start + 1:
            return []
        for number, line in enumerate(source, start):
            prefix = f"{number}: "
            if not line.startswith(prefix):
                return []
            body = line[len(prefix):]
            if lines.setdefault(number, body) != body:
                return []
        mapping.append(f"{symbol['id']} ({file} L{start}-{end})")

    # Multiple snapshots for a single path must remain distinguishable.
    if not files or len({file for file, _ in files}) != len(files):
        return []
    blocks = ["Symbols (file and inclusive line range):", *mapping]
    for (file, _), lines in files.items():
        start = end = 0
        body_lines: list[str] = []
        for number, line in sorted(lines.items()):
            if number != end + 1 or not body_lines:
                if body_lines:
                    blocks.append(f"--- {file} L{start}-{end} ---\n" + "\n".join(body_lines))
                start, body_lines = number, []
            end = number
            body_lines.append(line)
        if body_lines:
            blocks.append(f"--- {file} L{start}-{end} ---\n" + "\n".join(body_lines))
    return blocks


def format_mcp_response(action: str, payload: dict[str, Any], *, revision: bool = False) -> str:
    """Serialize MCP tool responses into compact, lossless text (~40-60% fewer tokens than indented JSON)."""
    if not isinstance(payload, dict) or "error" in payload:
        return json.dumps(payload, separators=(",", ":"))

    graph_text = format_graph_response(action, payload)
    if graph_text is not None:
        return graph_text
    if action in _CODE_MODE_ACTIONS:
        from runtime import code_mode

        cm_text = code_mode.format_response(action, payload)
        if cm_text is not None:
            return cm_text
    if action in _ANALYSIS_ACTIONS:
        from runtime import index_analysis

        an_text = index_analysis.format_response(action, payload)
        if an_text is not None:
            return an_text

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
            tags = []
            if item.get("defines"):
                tags.append("def")
            if item.get("importer"):
                tags.append("imp")
            tag_s = f" [{','.join(tags)}]" if tags else ""
            lines.append(f"  {item.get('file')} ({item.get('references')} refs){tag_s}")
        return "\n".join(lines)

    if action == "get_file_tree" and isinstance(payload.get("files"), list):
        from runtime import output_compressor

        files = [str(f) for f in payload["files"]]
        body = output_compressor.compact_file_list(files)
        return f"repo={payload.get('repo','')} ({len(files)} files):\n{body}"

    if action == "get_symbol_source" and isinstance(payload.get("source"), str):
        stamp = f" [rev={payload['_revision']}]" if revision and payload.get("_revision") else ""
        return f"{payload.get('id')} ({payload.get('file')} L{payload.get('start_line')}-{payload.get('end_line')}){stamp}:\n{payload['source']}"

    if action in ("get_symbols", "assemble_task_context") and isinstance(payload.get("symbols"), list):
        blocks: list[str] = []
        if action == "assemble_task_context":
            blocks.append(f"repo={payload.get('repo','')} (~{payload.get('approx_tokens', 0)} tok, {len(payload['symbols'])} symbols):")
        for s in payload["symbols"]:
            if isinstance(s, dict) and isinstance(s.get("source"), str):
                blocks.append(f"--- {s.get('id')} ({s.get('file')} L{s.get('start_line')}-{s.get('end_line')}) ---\n{s['source']}")
            elif isinstance(s, dict) and s.get("error"):
                blocks.append(str(s["error"]))
        if blocks:
            full = "\n".join(blocks)
            packed = _compact_source_batch(payload["symbols"]) if payload["symbols"] else []
            if packed:
                if action == "assemble_task_context":
                    packed.insert(0, f"repo={payload.get('repo','')} ({len(payload['symbols'])} symbols):")
                compact = "\n".join(packed)
                if len(compact) < len(full):
                    return compact
            return full

    if action == "search_sandbox" and isinstance(payload.get("result"), str):
        return payload["result"]

    if action == "expand_sandbox" and isinstance(payload.get("content"), str):
        return f"[Terser Sandbox `{payload.get('archive_id', '')}`]\n{payload['content']}"

    if action == "get_repo_outline" and isinstance(payload.get("outline"), list):
        outline = payload["outline"]
        header = f"repo={payload.get('repo','')} ({payload.get('files', 0)} files, {payload.get('symbols', 0)} symbols):"
        return header + ("\n" + "\n".join(str(x) for x in outline) if outline else " (empty)")

    if action == "list_repos" and isinstance(payload.get("repos"), list):
        repos = payload["repos"]
        if not repos:
            return "No indexed repositories."
        lines = [f"Indexed repositories ({len(repos)}):"]
        for r in repos:
            if isinstance(r, dict):
                lines.append(f"  {r.get('repo','')} ({r.get('files',0)} files, {r.get('symbols',0)} symbols) -> {r.get('source_root','')}")
        return "\n".join(lines)

    if action == "menu" and isinstance(payload.get("actions"), dict):
        actions = payload["actions"]
        lines = [f"Terser index actions ({len(actions)}):"]
        for k, v in actions.items():
            if isinstance(v, dict):
                lines.append(f"  {k} {v.get('args', '{}')} — {v.get('desc', '')}")
        return "\n".join(lines)

    if action == "execute_batch" and "batch" in payload:
        return str(payload["batch"])

    if action == "fetch_and_index" and "outline" in payload:
        from runtime import web_fetch

        return web_fetch.format_response(payload)

    if action == "execute_filtered" and "returncode" in payload:
        parts = [
            f"exit={payload.get('returncode', 0)} filtered={payload.get('filtered', False)} raw_bytes={payload.get('raw_bytes', 0)}"
            + (f" archive_id={payload['archive_id']}" if payload.get("archive_id") else "")
        ]
        stdout = payload.get("output", payload.get("stdout", ""))
        if stdout:
            parts.append(stdout)
        if payload.get("stderr"):
            parts.append(f"--- stderr ---\n{payload['stderr']}")
        return "\n".join(parts)

    return json.dumps(payload, separators=(",", ":"))


def _coerce_arg(value: Any, kind: str) -> Any:
    """Undo common model slips: stringified objects/arrays, numeric strings, integral floats, "true"."""
    if kind == "object" and isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else value
        except ValueError:
            return value
    if kind == "integer":
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and re.fullmatch(r"\s*-?\d+\s*", value):
            return int(value)
    if kind == "boolean" and isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    if kind == "array" and isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, list) else value
            except ValueError:
                return value
        return [part.strip() for part in text.split(",") if part.strip()]
    return value


def _coerce_order_args(args: dict[str, Any]) -> dict[str, Any]:
    out = dict(args)
    for key in ("limit", "token_budget", "start_line", "end_line", "depth"):
        if key in out:
            out[key] = _coerce_arg(out[key], "integer")
    for key in ("symbol_ids", "argv"):
        if key in out:
            out[key] = _coerce_arg(out[key], "array")
    return out


def _handle_tool_call(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if name == "menu":
        q = str(arguments.get("query") or "").lower()
        items = {k: v for k, v in _ACTIONS_MENU.items() if not q or q in k.lower() or q in v["desc"].lower()}
        return "menu", {"actions": items}
    if name == "route":
        task = str(arguments.get("task") or "").lower()
        if any(k in task for k in ("purge", "clear archive", "delete archive")):
            rec = "purge_sandbox"
        elif any(k in task for k in ("script", "codemod", "code mode", "parse every", "for each file")):
            rec = "run_script"
        elif any(k in task for k in ("batch edit", "edit several", "edit many", "multiple edits", "replace across", "bulk replace")):
            rec = "code_edit"
        elif any(k in task for k in ("files that contain", "but not", "files containing", "multi-pattern")):
            rec = "code_search"
        elif any(k in task for k in ("json value", "toml value", "config value", "dependencies of", "read key", "query json")):
            rec = "code_query"
        elif any(k in task for k in ("fetch url", "web page", "docs page", "fetch and index", "http://", "https://")):
            rec = "fetch_and_index"
        elif any(k in task for k in ("hotspot", "churn", "most changed")):
            rec = "get_hotspots"
        elif any(k in task for k in ("history of", "who changed", "when did", "blame")):
            rec = "get_symbol_history"
        elif any(k in task for k in ("untested", "no tests", "test coverage", "missing tests")):
            rec = "get_untested_symbols"
        elif any(k in task for k in ("implementations", "implements", "subclasses of", "implementers")):
            rec = "find_implementations"
        elif any(k in task for k in ("hierarchy", "inherit", "base class", "superclass")):
            rec = "get_class_hierarchy"
        elif any(k in task for k in ("duplicate", "similar code", "copy-paste", "clone")):
            rec = "find_similar_symbols"
        elif any(k in task for k in ("symbol diff", "how did", "changed since")):
            rec = "get_symbol_diff"
        elif any(k in task for k in ("pr risk", "risk of", "review risk", "risky change")):
            rec = "get_pr_risk_profile"
        elif any(k in task for k in ("layer", "architecture rule", "boundary violation")):
            rec = "get_layer_violations"
        elif any(k in task for k in ("workspace", "monorepo", "packages in")):
            rec = "list_workspaces"
        elif any(k in task for k in ("scip",)):
            rec = "import_scip"
        elif any(k in task for k in ("sandbox", "archive", "log output")):
            rec = "search_sandbox"
        elif any(k in task for k in ("execute", "run command", "filter output")):
            rec = "execute_filtered"
        elif "rename" in task:
            rec = "check_rename_safe"
        elif any(k in task for k in ("delete", "remove symbol")):
            rec = "check_delete_safe"
        elif any(k in task for k in ("safe to edit", "edit safe", "safe to change")):
            rec = "check_edit_safe"
        elif any(k in task for k in ("dead code", "unused", "unreferenced")):
            rec = "find_dead_code"
        elif any(k in task for k in ("cycle", "circular")):
            rec = "get_dependency_cycles"
        elif any(k in task for k in ("diff", "changed", "uncommitted")):
            rec = "get_changed_symbols"
        elif any(k in task for k in ("importer", "imports", "who imports")):
            rec = "find_importers"
        elif any(k in task for k in ("caller", "callee", "call hierarchy", "call graph")):
            rec = "get_call_hierarchy"
        elif any(k in task for k in ("usage", "used", "reference")):
            rec = "find_references"
        elif any(k in task for k in ("blast", "impact")):
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
        args = _coerce_arg(arguments.get("args", {}), "object")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ValueError("order.args must be an object")
        args = _coerce_order_args(args)
        if act not in _ACTIONS_MENU and act not in ("menu",):
            import difflib

            close = difflib.get_close_matches(act, list(_ACTIONS_MENU), n=3, cutoff=0.5)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            return act, {"error": f"Unknown action `{act}`.{hint} Call `menu` to list actions."}
        if any(type(args[key]) is not int for key in ("limit", "token_budget", "start_line", "end_line", "depth") if key in args):
            raise ValueError("limit, token_budget, start_line, end_line and depth must be integers")
        for key in ("symbol_ids", "argv"):
            if key in args and (not isinstance(args[key], list) or any(not isinstance(item, str) for item in args[key])):
                raise ValueError(f"{key} must be an array of strings")
        return act, dispatch_action(act, args)
    return name, dispatch_action(name, arguments)


def _rpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _handle_request(req: Any) -> dict[str, Any] | None:
    if not isinstance(req, dict) or req.get("jsonrpc") != "2.0" or not isinstance(req.get("method"), str):
        return _rpc_error(None, -32600, "Invalid request")
    if "id" not in req:
        return None  # JSON-RPC notifications never receive a response.
    req_id = req["id"]
    if req_id is not None and type(req_id) not in (str, int):
        return _rpc_error(None, -32600, "Invalid request id")
    method = req["method"]
    params = req.get("params", {})
    if not isinstance(params, dict):
        return _rpc_error(req_id, -32602, "params must be an object")

    if method == "initialize":
        requested = params.get("protocolVersion")
        caps: dict[str, Any] = {"tools": {}}
        if _mcp_apps_enabled():
            caps["resources"] = {}
        result = {
            "protocolVersion": requested if requested in ("2025-11-25", "2025-06-18", "2025-03-26") else PROTOCOL_VERSION,
            "capabilities": caps,
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "tools/list":
        result = {"tools": _active_mcp_tools()}
    elif method == "resources/list":
        if not _mcp_apps_enabled():
            result = {"resources": []}
        else:
            result = {
                "resources": [
                    {
                        "uri": _HUD_APP_URI,
                        "name": "Terser HUD",
                        "description": "Live Terser token savings, API usage, costs, quality grade, router, and engine controls.",
                        "mimeType": _HUD_APP_MIME,
                        "_meta": _HUD_APP_META,
                    }
                ]
            }
    elif method == "resources/read":
        uri = params.get("uri")
        if uri != _HUD_APP_URI or not _mcp_apps_enabled():
            return _rpc_error(req_id, -32602, f"Unknown resource URI: {uri}")
        try:
            html_doc = _read_hud_app_html()
        except OSError as exc:
            return _rpc_error(req_id, -32603, f"HUD UI asset unavailable: {exc}")
        result = {
            "contents": [
                {
                    "uri": _HUD_APP_URI,
                    "mimeType": _HUD_APP_MIME,
                    "text": html_doc,
                    "_meta": _HUD_APP_META,
                }
            ]
        }
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        tool = next((entry for entry in _active_mcp_tools() if entry["name"] == name), None)
        if tool is None or not isinstance(arguments, dict):
            return _rpc_error(req_id, -32602, "Expected a known tool name and an arguments object")
        schema = tool["inputSchema"]
        types = {"string": str, "integer": int, "object": dict, "boolean": bool}
        for key in schema.get("required", []):
            if key not in arguments:
                return _rpc_error(req_id, -32602, f"Missing required argument: {key}")
        arguments = dict(arguments)
        for key, field in schema["properties"].items():
            if key in arguments:
                arguments[key] = _coerce_arg(arguments[key], field["type"])
            if key in arguments and type(arguments[key]) is not types[field["type"]]:
                return _rpc_error(req_id, -32602, f"{key} must be of type {field['type']}")
        if name in ("terser_hud", "terser_hud_snapshot", "terser_hud_set_config"):
            try:
                result = _handle_hud_tool(name, arguments)
            except ValueError as exc:
                return _rpc_error(req_id, -32602, str(exc))
        else:
            try:
                action, payload = _handle_tool_call(name, arguments)
                inner = arguments.get("args") if name == "order" else None
                use_revision = isinstance(inner, dict) and inner.get("reuse_visible_result") is True
                message = format_mcp_response(action, payload, revision=use_revision)
            except ValueError as exc:
                return _rpc_error(req_id, -32602, str(exc))
            result = {"content": [{"type": "text", "text": message}]}
            if "error" in payload:
                result["isError"] = True
    elif method == "ping":
        result = {}
    else:
        return _rpc_error(req_id, -32601, f"Method not found: {method}")
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _serve_stdio() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp = _rpc_error(None, -32700, "Parse error")
        else:
            try:
                resp = _handle_request(req)
            except Exception as exc:
                print(f"{SERVER_NAME} request failed ({type(exc).__name__})", file=sys.stderr)
                resp = _rpc_error(req["id"], -32603, "Internal index error") if isinstance(req, dict) and "id" in req else None
        if resp is not None:
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
    if len(args) >= 2 and args[0] == "auto-index":
        # Background first-time index spawned by SessionStart; skip oversized repos.
        root = Path(os.path.expanduser(args[1])).resolve()
        cap = int(args[2]) if len(args) >= 3 and args[2].isdigit() else 5000
        if root.is_dir() and len(_list_source_files(root)) <= cap:
            index_folder(str(root))
        return 0
    return _serve_stdio()


if __name__ == "__main__":
    sys.exit(main())
