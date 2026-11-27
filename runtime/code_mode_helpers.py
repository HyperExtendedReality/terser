"""Helpers for Terser Code Mode scripts (available as ``t`` inside ``run_script``).

A Code Mode script runs locally and only what it prints reaches the agent's context.
These helpers make the common jobs one-liners, so the script stays short:

Search    t.files(glob="**/*.py")            list repo files (skips vendored/ignored dirs)
          t.grep(r"TODO|FIXME", glob="*.py")   -> [Hit(path, line, text)]
          t.count(r"deprecated", glob="*.ts")  -> {path: n}
Index     t.symbols("parse", kind="function") -> symbol dicts from the terser-index
          t.source("<symbol-id>")             exact source of one symbol
          t.refs("name")                      reference lines for a name
Parse     t.json(p) t.toml(p) t.csv(p) t.ini(p) t.py_ast(p) t.read(p, start, end) t.lines(p)
Edit      t.replace(p, old, new, count=1)     exact text (errors when missing/ambiguous)
          t.sub(p, pattern, repl, count=0)    regex replace
          t.write(p, text)                    create or overwrite
          t.edit_symbol("<symbol-id>", new_source)  replace one symbol's whole body
Output    t.out(obj)  t.table(rows, cols)  t.diffstat()

Edits are staged in memory. With ``apply=false`` (the default) the script ends by
printing a compact unified diff of what would change. With ``apply=true`` the files
are written (line endings kept) and each one is reported as
``[terser-code-mode] edited: <path>`` so Terser can refresh its read cache and index.
Paths are relative to the repo root unless absolute; writes outside the root are refused.
Files matching ``.contextignore`` are skipped by searches and refused by reads.
"""
from __future__ import annotations

import ast
import csv as _csv
import configparser
import difflib
import fnmatch
import io
import json as _json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable, NamedTuple

_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", "dist", "build",
    "target", ".next", ".nuxt", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".terser", ".idea", ".vs",
})
_MAX_READ_BYTES = 4 * 1024 * 1024
_MAX_FILES = 20_000
_DIFF_MAX_LINES = 400

ROOT: Path = Path.cwd()
APPLY = False
_REPO = ""
_staged: dict[Path, tuple[str, str]] = {}  # path -> (original text, new text)
_bytes_read = 0
_files_cache: list[str] | None = None


class Hit(NamedTuple):
    path: str
    line: int
    text: str

    def __str__(self) -> str:  # pragma: no cover - formatting helper
        return f"{self.path}:{self.line}: {self.text}"


class CodeModeError(Exception):
    pass


def _init(root: str, apply: bool, repo: str = "") -> None:
    global ROOT, APPLY, _REPO, _bytes_read, _files_cache
    ROOT = Path(root).resolve()
    APPLY = bool(apply)
    _REPO = repo
    _staged.clear()
    _bytes_read = 0
    _files_cache = None


def _resolve(path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    p = (p if p.is_absolute() else ROOT / p).resolve()
    return p


def _rel(p: Path) -> str:
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def _ignored(p: Path) -> str | None:
    try:
        from runtime import user_filters

        return user_filters.ignored_by(str(p), str(ROOT))
    except Exception:
        return None


def _read_text(p: Path) -> str:
    global _bytes_read
    if p in _staged:
        return _staged[p][1]
    pat = _ignored(p)
    if pat:
        raise CodeModeError(f"{_rel(p)} matches .contextignore pattern {pat}")
    size = p.stat().st_size
    if size > _MAX_READ_BYTES:
        raise CodeModeError(f"{_rel(p)} is {size} bytes (limit {_MAX_READ_BYTES})")
    raw = p.read_bytes()
    _bytes_read += len(raw)
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _git_files() -> list[str] | None:
    import subprocess

    try:
        kw: dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True, timeout=15, stdin=subprocess.DEVNULL, **kw,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return [f for f in proc.stdout.decode("utf-8", errors="replace").split("\0") if f]


def _walk_files() -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            out.append(Path(dirpath, name).relative_to(ROOT).as_posix())
            if len(out) >= _MAX_FILES:
                return out
    return out


def files(glob: str | Iterable[str] = "**/*", *, exts: Iterable[str] | None = None) -> list[str]:
    """Repo-relative paths (git-tracked + untracked-not-ignored, else a filtered walk)."""
    global _files_cache
    if _files_cache is None:
        listed = _git_files()
        base = listed if listed is not None else _walk_files()
        _files_cache = [
            f for f in base
            if not any(part in _SKIP_DIRS for part in f.split("/")[:-1]) and not _ignored(ROOT / f)
        ][:_MAX_FILES]
    globs = [glob] if isinstance(glob, str) else list(glob)
    ext_set = {e if e.startswith(".") else "." + e for e in exts} if exts else None
    out = []
    for f in _files_cache:
        if ext_set and Path(f).suffix not in ext_set:
            continue
        name = f.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatch(f, g) or ("/" not in g and fnmatch.fnmatch(name, g)) or (g.startswith("**/") and fnmatch.fnmatch(f, g[3:])) for g in globs):
            out.append(f)
    return out


def grep(
    pattern: str,
    glob: str | Iterable[str] = "**/*",
    *,
    flags: int = 0,
    ignore_case: bool = False,
    max_hits: int = 500,
    fixed: bool = False,
) -> list[Hit]:
    rx = re.compile(re.escape(pattern) if fixed else pattern, flags | (re.IGNORECASE if ignore_case else 0))
    hits: list[Hit] = []
    for f in files(glob):
        try:
            text = _read_text(ROOT / f)
        except (OSError, CodeModeError):
            continue
        if "\0" in text[:4096]:
            continue  # binary
        if not rx.search(text):
            continue
        for no, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                hits.append(Hit(f, no, line.strip()[:240]))
                if len(hits) >= max_hits:
                    return hits
    return hits


def count(pattern: str, glob: str | Iterable[str] = "**/*", **kw: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for h in grep(pattern, glob, max_hits=10**9, **kw):
        out[h.path] = out.get(h.path, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


# ---------------------------------------------------------------------------
# Index (terser-index)
# ---------------------------------------------------------------------------

def _index() -> Any:
    from runtime import index_mcp

    return index_mcp


def _repo() -> str:
    """Index slug for ROOT (empty -> terser-index resolves it from the working directory)."""
    return _REPO


def symbols(query: str = "", *, kind: str | None = None, file: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    idx = _index()
    if file:
        res = idx.dispatch_action("get_file_outline", {"repo": _repo(), "file_path": file})
        syms = res.get("symbols") or []
    else:
        res = idx.dispatch_action("search_symbols", {"repo": _repo(), "query": query, "limit": limit})
        syms = res.get("matches") or []
    if "error" in res:
        raise CodeModeError(res["error"])
    if kind:
        syms = [s for s in syms if str(s.get("kind", "")).lower() == kind.lower()]
    return syms[:limit]


def source(symbol_id: str) -> str:
    res = _index().dispatch_action("get_symbol_source", {"repo": _repo(), "symbol_id": symbol_id})
    if "error" in res:
        raise CodeModeError(res["error"])
    return "\n".join(re.sub(r"^\d+: ", "", ln, count=1) for ln in str(res.get("source") or "").split("\n"))


def refs(name: str) -> list[dict[str, Any]]:
    res = _index().dispatch_action("find_references", {"repo": _repo(), "symbol": name})
    if "error" in res:
        raise CodeModeError(res["error"])
    return list(res.get("references") or [])


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def read(path: str, start: int | None = None, end: int | None = None) -> str:
    text = _read_text(_resolve(path))
    if start is None and end is None:
        return text
    lines = text.splitlines(keepends=True)
    s = max(1, start or 1)
    e = min(len(lines), end or len(lines))
    return "".join(lines[s - 1:e])


def lines(path: str) -> list[str]:
    return _read_text(_resolve(path)).splitlines()


def json(path: str) -> Any:  # noqa: A001 - helper name mirrors the format
    return _json.loads(_read_text(_resolve(path)))


def toml(path: str) -> dict[str, Any]:
    from runtime import user_filters

    return user_filters.parse_toml(_read_text(_resolve(path)))


def csv_rows(path: str, **kw: Any) -> list[dict[str, str]]:
    return list(_csv.DictReader(io.StringIO(_read_text(_resolve(path))), **kw))


csv = csv_rows  # noqa: F811 - `t.csv(path)` reads like the other parsers


def ini(path: str) -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.read_string(_read_text(_resolve(path)))
    return cp


def py_ast(path: str) -> ast.Module:
    return ast.parse(_read_text(_resolve(path)), filename=str(path))


# ---------------------------------------------------------------------------
# Edit (staged; applied only when apply=true)
# ---------------------------------------------------------------------------

def _stage(p: Path, new_text: str) -> None:
    try:
        p.relative_to(ROOT)
    except ValueError:
        raise CodeModeError(f"refusing to write outside the repo root: {p}") from None
    pat = _ignored(p)
    if pat:
        raise CodeModeError(f"{_rel(p)} matches .contextignore pattern {pat}")
    if p in _staged:
        _staged[p] = (_staged[p][0], new_text)
        return
    original = ""
    if p.is_file():
        original = p.read_bytes().decode("utf-8", errors="strict")
    _staged[p] = (original, new_text)


def _current(p: Path) -> str:
    if p in _staged:
        return _staged[p][1]
    return p.read_bytes().decode("utf-8", errors="strict") if p.is_file() else ""


def replace(path: str, old: str, new: str, count: int = 1) -> int:
    """Exact replacement. ``count=1`` requires exactly one match; ``count=0`` replaces all."""
    p = _resolve(path)
    text = _current(p)
    n = text.count(old)
    if n == 0:
        raise CodeModeError(f"{_rel(p)}: text not found: {old[:80]!r}")
    if count and n != count:
        raise CodeModeError(f"{_rel(p)}: expected {count} match(es), found {n}: {old[:80]!r}")
    _stage(p, text.replace(old, new) if not count else text.replace(old, new, count))
    return n if not count else count


def sub(path: str, pattern: str, repl: str, count: int = 0, flags: int = 0) -> int:
    p = _resolve(path)
    text = _current(p)
    new_text, n = re.subn(pattern, repl, text, count=count, flags=flags)
    if n:
        _stage(p, new_text)
    return n


def write(path: str, text: str) -> None:
    _stage(_resolve(path), text)


def edit_symbol(symbol_id: str, new_source: str) -> None:
    """Replace a whole indexed symbol (its exact line span) with ``new_source``."""
    res = _index().dispatch_action("get_symbol_source", {"repo": _repo(), "symbol_id": symbol_id})
    if "error" in res:
        raise CodeModeError(res["error"])
    p = _resolve(str(res.get("file") or ""))
    start, end = int(res["start_line"]), int(res["end_line"])
    text = _current(p)
    nl = "\r\n" if "\r\n" in text else "\n"
    body = text.splitlines(keepends=True)
    replacement = new_source.replace("\r\n", "\n").rstrip("\n").replace("\n", nl) + nl
    body[start - 1:end] = [replacement]
    _stage(p, "".join(body))


def _diff(p: Path, before: str, after: str) -> list[str]:
    return list(difflib.unified_diff(
        before.splitlines(), after.splitlines(), f"a/{_rel(p)}", f"b/{_rel(p)}", n=1, lineterm="",
    ))


def diffstat() -> list[str]:
    rows = []
    for p, (before, after) in _staged.items():
        if before == after:
            continue
        d = _diff(p, before, after)
        plus = sum(1 for ln in d if ln.startswith("+") and not ln.startswith("+++"))
        minus = sum(1 for ln in d if ln.startswith("-") and not ln.startswith("---"))
        rows.append(f"{_rel(p)} (+{plus} -{minus}){' new' if not before else ''}")
    return rows


def _write_atomic(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.terser-tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, p)


def _finish(stream: Any = None) -> list[str]:
    """Print the staged-edit summary (and write when APPLY). Returns edited paths."""
    stream = stream or sys.stdout
    changed = [(p, b, a) for p, (b, a) in _staged.items() if b != a]
    edited: list[str] = []
    if changed:
        if APPLY:
            for p, _, after in changed:
                _write_atomic(p, after)
                edited.append(str(p))
            print("\n[terser-code-mode] applied: " + "; ".join(diffstat()), file=stream)
            for path in edited:
                print(f"[terser-code-mode] edited: {path}", file=stream)
        else:
            out: list[str] = []
            for p, before, after in changed:
                out.extend(_diff(p, before, after))
            if len(out) > _DIFF_MAX_LINES:
                out = out[:_DIFF_MAX_LINES] + [f"... [{len(out) - _DIFF_MAX_LINES} more diff lines]"]
            print("\n[terser-code-mode] dry run, nothing written. Re-run with apply=true to write:", file=stream)
            print("\n".join(out), file=stream)
    print(f"[terser-code-mode] read_bytes={_bytes_read}", file=stream)
    return edited


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def out(obj: Any) -> None:
    if isinstance(obj, str):
        print(obj)
    elif isinstance(obj, (list, tuple)) and obj and all(isinstance(x, Hit) for x in obj):
        cur = None
        for h in obj:
            if h.path != cur:
                print(f"{h.path}:")
                cur = h.path
            print(f"  {h.line}: {h.text}")
    else:
        print(_json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str))


def table(rows: Iterable[Any], cols: list[str] | None = None) -> None:
    rows = list(rows)
    if not rows:
        print("(no rows)")
        return
    if isinstance(rows[0], dict):
        cols = cols or list(rows[0].keys())
        print("\t".join(cols))
        for r in rows:
            print("\t".join(str(r.get(c, "")) for c in cols))
    else:
        if cols:
            print("\t".join(cols))
        for r in rows:
            print("\t".join(str(x) for x in (r if isinstance(r, (list, tuple)) else [r])))
