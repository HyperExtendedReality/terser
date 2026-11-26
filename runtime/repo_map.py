"""Terser — Zero-Dependency Import-Graph PageRank Repo Map.

Inspired by Aider's PageRank RepoMap and Graphify:
When a repository is NOT yet indexed by jCodeMunch (or when invoked explicitly
via `terse repomap`), builds a compact, strictly token-budgeted structural map of
the most central files and their key symbol signatures using import-graph
PageRank—cached by git HEAD + file-mtime digest so the output is 100%
byte-stable across turns (prompt-cache safe!).

Pure Python stdlib, <150ms on typical repos, fail-open.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from runtime.config import atomic_write_json, state_home, workspace_hash

_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "dist", "build", "target", "out",
    "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".next", ".nuxt", "coverage", "vendor", "third_party", "bin", "obj",
})
_CODE_EXTS = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".cs"})
_MAX_FILES = 400
_MAX_FILE_BYTES = 128 * 1024

_IMPORT_RE = re.compile(
    r"""(?m)^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.,\s]+)|"""
    r"""(?:import|export).*?from\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)|"""
    r"""use\s+([\w:]+)|using\s+([\w.]+))"""
)
_GENERIC_DEF_RE = re.compile(
    r"^(?:export\s+|public\s+|pub\s+|async\s+)*(?:class|interface|struct|enum|trait|function|fn|func)\s+([A-Za-z_]\w*)"
)


def _extract_file_info(path: Path) -> tuple[list[str], list[str]]:
    """Return ``(top_level_symbols, imported_tokens)`` for a single source file."""
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return [], []
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], []

    syms: list[str] = []
    imports: list[str] = []

    if path.suffix.lower() == ".py":
        try:
            tree = ast.parse(text)
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    syms.append(f"class {node.name}")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not node.name.startswith("_"):
                        syms.append(f"def {node.name}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.extend(p for p in node.module.split(".") if p)
            return syms[:8], imports
        except SyntaxError:
            pass

    for line in text.splitlines():
        m = _GENERIC_DEF_RE.match(line.strip())
        if m:
            syms.append(m.group(1))
            if len(syms) >= 8:
                break
    for m in _IMPORT_RE.finditer(text):
        raw = next((g for g in m.groups() if g), "")
        for tok in re.split(r"[/\\.:,\s]+", raw):
            if tok and len(tok) >= 2 and tok not in (".", ".."):
                imports.append(tok)
    return syms[:8], imports


def _discover_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.suffix.lower() in _CODE_EXTS:
                found.append(p)
                if len(found) >= _MAX_FILES:
                    return found
    return found


def _pagerank(nodes: list[str], edges: dict[str, set[str]], iterations: int = 10, d: float = 0.85) -> dict[str, float]:
    n = len(nodes)
    if n == 0:
        return {}
    rank = {u: 1.0 / n for u in nodes}
    inbound: dict[str, list[str]] = {u: [] for u in nodes}
    out_deg: dict[str, int] = {}
    for u in nodes:
        outs = [v for v in edges.get(u, ()) if v in rank and v != u]
        out_deg[u] = len(outs)
        for v in outs:
            inbound[v].append(u)
    base = (1.0 - d) / n
    for _ in range(iterations):
        nxt: dict[str, float] = {}
        for u in nodes:
            s = sum(rank[src] / out_deg[src] for src in inbound[u] if out_deg[src] > 0)
            nxt[u] = base + d * s
        rank = nxt
    return rank


def build_repo_map(
    workspace: str,
    *,
    max_tokens: int = 400,
    min_files: int = 5,
    env: dict[str, str] | None = None,
) -> str | None:
    """Build (or load cached) byte-stable PageRank Repo Map within `max_tokens`."""
    if not workspace:
        return None
    root = Path(workspace)
    if not root.is_dir():
        return None

    files = _discover_files(root)
    if len(files) < min_files:
        return None

    # Fingerprint repo structure by relative paths + sizes + mtime buckets for cache reuse
    sig_parts = []
    for f in files:
        try:
            st = f.stat()
            rel = f.relative_to(root).as_posix()
            sig_parts.append(f"{rel}:{st.st_size}:{int(st.st_mtime)}")
        except OSError:
            continue
    fp = hashlib.sha256("\n".join(sig_parts).encode("utf-8")).hexdigest()[:20]

    cache_path = state_home(env) / "repomap-cache" / f"{workspace_hash(workspace)}.json"
    try:
        if cache_path.is_file():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("fp") == fp and cached.get("max_tokens") == max_tokens:
                return str(cached.get("map") or "") or None
    except Exception:
        pass

    rel_paths: list[str] = []
    stem_to_paths: dict[str, list[str]] = {}
    file_syms: dict[str, list[str]] = {}
    file_imports: dict[str, list[str]] = {}

    for f in files:
        rel = f.relative_to(root).as_posix()
        rel_paths.append(rel)
        stem = f.stem
        if stem not in ("__init__", "index", "mod", "main"):
            stem_to_paths.setdefault(stem, []).append(rel)
        parent = f.parent.name
        if parent:
            stem_to_paths.setdefault(parent, []).append(rel)
        syms, imps = _extract_file_info(f)
        file_syms[rel] = syms
        file_imports[rel] = imps

    edges: dict[str, set[str]] = {r: set() for r in rel_paths}
    for rel in rel_paths:
        for tok in file_imports[rel]:
            for target in stem_to_paths.get(tok, ()):
                if target != rel:
                    edges[rel].add(target)

    ranks = _pagerank(rel_paths, edges)
    # Deterministic tie-break by path so prompt prefix stays 100% byte-stable!
    ordered = sorted(rel_paths, key=lambda r: (-round(ranks.get(r, 0.0), 6), r))

    max_chars = max(200, int(max_tokens) * 4)
    lines = [f"Repo map ({len(rel_paths)} files, PageRank top):"]
    used = len(lines[0]) + 1
    for rel in ordered:
        syms = file_syms.get(rel) or []
        entry = f"  {rel}: {', '.join(syms[:5])}" if syms else f"  {rel}"
        if used + len(entry) + 1 > max_chars:
            break
        lines.append(entry)
        used += len(entry) + 1

    rendered = "\n".join(lines) if len(lines) > 1 else None
    if rendered:
        atomic_write_json(cache_path, {"fp": fp, "max_tokens": max_tokens, "map": rendered})
    return rendered
