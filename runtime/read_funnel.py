"""Terser — 4-Tier Quality-Preserving Read Funnel & MCP Refetch Guard.

Unifies Terser's Read Cache + Delta Diff + Structure Map with symbol-first
AST navigation into one conflict-free PreToolUse funnel:

  Tier 0 (Unchanged re-read):
      Soft-deny ONCE with a concise pointer to the already-loaded context.
      Immediate retry always passes through (safe across /compact).
  Tier 1 (Small-edit re-read):
      Return exact unified diff (<= 60 lines & < 40% of slice) ONCE instead of
      re-reading the entire file. Immediate retry returns the full file.
  Tier 1b (Larger change confined to a few symbols):
      Return those whole symbols, bounded by unchanged anchor lines, ONCE.
  Tier 2 (First unranged read of a LARGE code file >= 10 KB & >= 250 lines):
      Return an inline, line-numbered AST/structure outline (imports + class/fn
      signatures + exact line spans) ONCE, pointing to ranged reads or `terser-index`
      `order {action: "get_symbol_source"}`. Immediate retry returns the full file!
  Tier 3 (Small/medium file, ranged read, non-code file, ByteOffset read, or retry):
      Pass straight through untouched with zero friction.

Also includes a 120-second read-only MCP duplicate-call guard (soft-deny once).
Pure Python stdlib, atomic file writes, fail-open everywhere.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from runtime.config import (
    atomic_write_json,
    ensure_vendor_paths,
    record_funnel_event,
    tier_backed_off,
    record_savings,
    state_home,
)

_MAX_FILE_BYTES = 1024 * 1024       # 1 MB safety cap
_MAX_SNAPSHOT_BYTES = 128 * 1024    # 128 KB delta-diff snapshot cap
_MAX_STORE_ENTRIES = 160
_STORE_TTL_SECONDS = 7 * 86400
_SOFT_DENY_CAP = 1                  # Always allow on immediate retry!
_DELTA_MAX_DIFF_LINES = 60
_DELTA_MAX_RATIO = 0.40
_MAX_OUTLINE_LINES = 90
_LARGE_SLICE_MIN_LINES = 400
_LARGE_SLICE_MIN_BYTES = 12288

_SID_RE = re.compile(r"^[0-9a-zA-Z_.-]{1,96}$")

_CODE_EXTS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
    ".rb", ".php", ".swift", ".scala", ".lua", ".dart", ".zig", ".gd",
})

_MUTATING_OR_LIVE_MCP_TOKENS = frozenset({
    "write", "edit", "create", "delete", "remove", "drop", "update", "patch",
    "put", "post", "send", "submit", "publish", "deploy", "run", "exec",
    "execute", "invoke", "trigger", "start", "stop", "kill", "cancel",
    "status", "poll", "wait", "watch", "tail", "logs", "time", "now",
    "clock", "health", "ping", "refresh", "invalidate", "register", "index",
    # UI / browser / device / session tools whose result reflects state other calls change.
    "computer", "screenshot", "screen", "snapshot", "capture", "page", "tab", "tabs",
    "navigate", "click", "scroll", "type", "key", "hover", "drag", "console", "network",
    "javascript", "eval", "evaluate", "messages", "inbox", "notifications", "events",
    "current", "latest", "recent", "live", "session", "sessions", "terminal", "clipboard",
    "query", "sql", "select",
})
# Server names whose tools observe or drive live state (browsers, desktops, devices, debuggers).
_LIVE_MCP_SERVER_TOKENS = frozenset({
    "browser", "chrome", "playwright", "puppeteer", "selenium", "computer", "desktop",
    "terminal", "devtools", "screen", "android", "adb", "ios", "simulator", "emulator",
    "frida", "gdb", "debug", "debugger", "rizin", "rea",
})

# Regex fallback for non-Python code outlines when runtime.code_outline finds too few symbols
_GENERIC_SYM_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?:export\s+|public\s+|private\s+|protected\s+|static\s+|async\s+|pub\s+|override\s+|virtual\s+)*"
    r"(?:class|interface|struct|enum|trait|impl|function|fn|func|def|type|const\s+\w+\s*=\s*(?:async\s*)?\()"
    r"\s+([A-Za-z_][\w$.<>]*)"
)


def _cache_dir(env: dict[str, str] | None = None) -> Path:
    return state_home(env) / "read-cache"


def _outline_cache_path(sha: str, env: dict[str, str] | None = None) -> Path:
    return state_home(env) / "outline-cache" / f"{sha[:24]}.txt"


def _load_cached_outline(sha: str, env: dict[str, str] | None = None) -> str | None:
    p = _outline_cache_path(sha, env)
    try:
        if p.is_file() and p.stat().st_size <= 65536:
            text = p.read_text(encoding="utf-8", errors="replace").strip()
            return text or None
    except OSError:
        pass
    return None


def _save_cached_outline(sha: str, outline: str, env: dict[str, str] | None = None) -> None:
    if not sha or not outline:
        return
    p = _outline_cache_path(sha, env)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".tmp.{os.getpid()}")
        tmp.write_text(outline, encoding="utf-8", errors="replace")
        os.replace(tmp, p)
    except OSError:
        pass


def _cache_path(session_id: str, env: dict[str, str] | None = None) -> Path:
    safe_sid = re.sub(r"[^0-9a-zA-Z_.-]", "_", session_id)[:80]
    return _cache_dir(env) / f"{safe_sid}.json"


_SWEEP_INTERVAL_SECONDS = 3600
_SWEEP_MARKER = ".last-sweep"
_BLOB_DIR = "blobs"
_BLOB_NAME_RE = re.compile(r"^[0-9a-f]{64}$")


def _sweep_stale(cdir: Path, now: float) -> None:
    """Delete session stores and snapshot blobs untouched for the TTL, at most once per hour.

    Live blobs are touched each time a store references them, so only unreferenced or
    long-idle blobs age out. A missing blob only costs one delta tier (the read falls back to T3).
    """
    try:
        if not cdir.is_dir():
            return
        marker = cdir / _SWEEP_MARKER
        try:
            if now - marker.stat().st_mtime < _SWEEP_INTERVAL_SECONDS:
                return
        except OSError:
            pass
        try:
            marker.touch()
        except OSError:
            pass
        cutoff = now - _STORE_TTL_SECONDS
        for pattern, base in (("*.json", cdir), ("*.txt", cdir / _BLOB_DIR)):
            if not base.is_dir():
                continue
            for p in base.glob(pattern):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    continue
    except OSError:
        pass


def _blob_path(digest: str, env: dict[str, str] | None = None) -> Path:
    return _cache_dir(env) / _BLOB_DIR / f"{digest}.txt"


def _write_blob(text: str, env: dict[str, str] | None = None) -> str | None:
    """Store `text` content-addressed (sha256 of its UTF-8 bytes); returns the digest."""
    data = text.encode("utf-8", errors="surrogatepass")
    digest = hashlib.sha256(data).hexdigest()
    p = _blob_path(digest, env)
    try:
        if p.is_file():
            os.utime(p, None)
            return digest
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{digest}.tmp.{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        return digest
    except OSError:
        return None


def _snapshot_text(entry: dict[str, Any], env: dict[str, str] | None = None) -> str | None:
    """Return an entry's cached snapshot: inline (legacy/in-flight) or from its verified blob."""
    inline = entry.get("snapshot")
    if isinstance(inline, str):
        return inline
    digest = entry.get("snapshot_sha")
    if not isinstance(digest, str) or not _BLOB_NAME_RE.match(digest):
        return None
    try:
        data = _blob_path(digest, env).read_bytes()
    except OSError:
        return None
    if hashlib.sha256(data).hexdigest() != digest:
        return None  # corrupt or truncated blob: never diff against it
    return data.decode("utf-8", errors="surrogatepass")


def _drop_snapshot(entry: dict[str, Any]) -> None:
    entry.pop("snapshot", None)
    entry.pop("snapshot_sha", None)


def _externalize_snapshots(store: dict, env: dict[str, str] | None = None) -> None:
    """Move inline snapshots into shared blobs so each hook parses and rewrites a small store."""
    reads = store.get("reads")
    if not isinstance(reads, dict):
        return
    for entry in reads.values():
        if not isinstance(entry, dict):
            continue
        snap = entry.get("snapshot")
        if not isinstance(snap, str):
            continue
        digest = _write_blob(snap, env)
        if digest:
            entry.pop("snapshot", None)
            entry["snapshot_sha"] = digest


def _load_store(session_id: str, env: dict[str, str] | None = None) -> dict[str, dict]:
    p = _cache_path(session_id, env)
    try:
        if not p.is_file():
            return {"reads": {}, "mcp": {}, "meta": {}, "searches": {}}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"reads": {}, "mcp": {}, "meta": {}, "searches": {}}
        return {
            "reads": data.get("reads") if isinstance(data.get("reads"), dict) else {},
            "mcp": data.get("mcp") if isinstance(data.get("mcp"), dict) else {},
            "meta": data.get("meta") if isinstance(data.get("meta"), dict) else {},
            "searches": data.get("searches") if isinstance(data.get("searches"), dict) else {},
        }
    except (OSError, json.JSONDecodeError, ValueError):
        return {"reads": {}, "mcp": {}, "meta": {}, "searches": {}}


def _save_store(session_id: str, store: dict, now: float, env: dict[str, str] | None = None) -> None:
    cdir = _cache_dir(env)
    _sweep_stale(cdir, now)
    for bucket_key in ("reads", "mcp", "searches"):
        bucket = store.get(bucket_key)
        if isinstance(bucket, dict) and len(bucket) > _MAX_STORE_ENTRIES:
            ordered = sorted(
                bucket.items(),
                key=lambda kv: kv[1].get("ts", 0) if isinstance(kv[1], dict) else 0,
                reverse=True,
            )[:_MAX_STORE_ENTRIES]
            store[bucket_key] = dict(ordered)
    _externalize_snapshots(store, env)
    atomic_write_json(_cache_path(session_id, env), store)


def clear_session_cache(session_id: str, env: dict[str, str] | None = None) -> None:
    """Clear read cache for a session (called on /compact or /clear)."""
    if not session_id:
        return
    try:
        _cache_path(session_id, env).unlink(missing_ok=True)
    except OSError:
        pass


def _norm_path(raw: str) -> str:
    try:
        p = str(Path(raw).resolve(strict=False))
    except (OSError, ValueError):
        p = str(raw)
    return p.lower() if os.name == "nt" else p


def _parse_int(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _range_covered(
    prev_start: int | None,
    prev_end: int | None,
    req_start: int | None,
    req_end: int | None,
) -> bool:
    """True when the requested span lies inside the span already delivered (None = open end)."""
    inf = float("inf")
    have = (1 if prev_start is None else prev_start, inf if prev_end is None else prev_end)
    want = (1 if req_start is None else req_start, inf if req_end is None else req_end)
    return have[0] <= want[0] and want[1] <= have[1]


def _merge_intervals(
    existing: list[Any] | None,
    new_start: int | None,
    new_end: int | None,
) -> list[list[int | None]]:
    """Merge a new 1-based inclusive [start, end] range into a sorted list of disjoint intervals."""
    items: list[tuple[int, int | None]] = []
    if isinstance(existing, list):
        for iv in existing:
            if isinstance(iv, (list, tuple)) and len(iv) == 2:
                s = _parse_int(iv[0])
                e = _parse_int(iv[1])
                items.append((max(1, s) if s is not None else 1, e))
    ns = max(1, new_start) if new_start is not None else 1
    ne = new_end
    items.append((ns, ne))
    items.sort(key=lambda x: (x[0], float("inf") if x[1] is None else x[1]))

    merged: list[list[int | None]] = []
    for s, e in items:
        if not merged:
            merged.append([s, e])
            continue
        ps, pe = merged[-1]
        if pe is None:
            continue
        if s <= pe + 1:
            merged[-1][1] = None if e is None else max(pe, e)
        else:
            merged.append([s, e])
    return merged


def _uncovered_subranges(
    existing: list[Any] | None,
    req_start: int | None,
    req_end: int | None,
    total_lines: int,
) -> tuple[list[tuple[int, int]], int]:
    """Return ``(uncovered_spans, covered_line_count)`` for ``[req_start, req_end]``."""
    rs = max(1, req_start) if req_start is not None else 1
    re_ = min(total_lines, req_end) if req_end is not None else total_lines
    if rs > re_:
        return [], 0

    cov: list[tuple[int, int]] = []
    if isinstance(existing, list):
        for iv in existing:
            if isinstance(iv, (list, tuple)) and len(iv) == 2:
                s = _parse_int(iv[0]) or 1
                e = _parse_int(iv[1])
                cs = max(rs, s)
                ce = min(re_, total_lines if e is None else e)
                if cs <= ce:
                    cov.append((cs, ce))
    if not cov:
        return [(rs, re_)], 0
    cov.sort()

    uncovered: list[tuple[int, int]] = []
    covered_lines = 0
    cursor = rs
    for cs, ce in cov:
        if cs > cursor:
            uncovered.append((cursor, cs - 1))
        if ce >= cursor:
            covered_lines += ce - max(cursor, cs) + 1
            cursor = ce + 1
    if cursor <= re_:
        uncovered.append((cursor, re_))
    return uncovered, covered_lines


def _intervals_cover(
    prev: dict[str, Any],
    req_start: int | None,
    req_end: int | None,
) -> bool:
    ivs = prev.get("intervals")
    if isinstance(ivs, list) and ivs:
        for iv in ivs:
            if isinstance(iv, (list, tuple)) and len(iv) == 2:
                ps = _parse_int(iv[0])
                pe = _parse_int(iv[1])
                if _range_covered(ps, pe, req_start, req_end):
                    return True
        return False
    return _range_covered(prev.get("start"), prev.get("end"), req_start, req_end)


def _slice_lines(text: str, start_line: int | None, end_line: int | None) -> list[str]:
    """1-based inclusive line window of ``text`` (line endings kept)."""
    first = max(1, start_line or 1)
    window = text.splitlines(keepends=True)
    return window[first - 1 : end_line] if end_line else window[first - 1 :]


def _compact_unified_diff(
    old_text: str,
    new_text: str,
    basename: str,
    start_line: int | None,
    end_line: int | None,
) -> str | None:
    """Unified diff of the requested window, or None when a diff would not be worth sending."""
    before = _slice_lines(old_text, start_line, end_line)
    after = _slice_lines(new_text, start_line, end_line)
    if not after:
        return None
    hunks = list(difflib.unified_diff(before, after, f"a/{basename}", f"b/{basename}", n=2))
    too_long = len(hunks) > _DELTA_MAX_DIFF_LINES
    too_dense = len(after) >= 20 and len(hunks) > _DELTA_MAX_RATIO * len(after)
    if not hunks or too_long or too_dense:
        return None
    return "".join(hunks).rstrip()


def _format_range(start_line: int | None, end_line: int | None) -> str:
    """Human label for a read span."""
    if end_line is None:
        return "full file" if start_line is None else f"from line {start_line}"
    return f"lines {1 if start_line is None else start_line}-{end_line}"


_DOC_EXTS = frozenset({".md", ".mdx", ".markdown"})
_DATA_EXTS = frozenset({".json", ".yaml", ".yml", ".toml"})
_LOG_EXTS = frozenset({".log", ".ndjson", ".jsonl", ".csv", ".tsv"})
_LOCKFILE_NAME_RE = re.compile(
    r"(?i)(?:^(?:package-lock\.json|pnpm-lock\.yaml|yarn\.lock|cargo\.lock|"
    r"poetry\.lock|uv\.lock|pipfile\.lock|composer\.lock|gemfile\.lock|go\.sum)$|"
    r"\.(?:min\.js|min\.css|map|snap)$)"
)
_MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*$")
_YAML_TOP_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$")
_TOML_SEC_RE = re.compile(r"^(\[\[?.+?\]?\])$")
_LOG_SIGNAL_RE = re.compile(r"(?i)\b(?:error|err|fatal|panic|exception|traceback|failed|fail|warn|warning|critical)\b")


def _outline_structured_data(file_path: str, text: str, *, env: dict[str, str] | None = None) -> str | None:
    """Tier 2c: Build compact line-numbered outlines for large lockfiles, JSON/YAML/TOML, and logs/CSV."""
    p = Path(file_path)
    ext = p.suffix.lower()
    lines = text.splitlines()

    # Lockfiles / minified / generated bundles
    if _LOCKFILE_NAME_RE.search(p.name):
        preview = "\n".join(f"L{i}: {ln[:100]}" for i, ln in enumerate(lines[:5], start=1))
        return (
            f"[Lockfile/generated file: {len(lines):,} lines — avoid full dumps; "
            f"use grep or ranged reads for specific packages.]\n{preview}"
        )

    # Large JSON -> top-level keys with approximate line numbers & sizes
    if ext == ".json":
        try:
            parsed = json.loads(text)
        except (ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict) and len(parsed) >= 2:
            key_lines: dict[str, int] = {}
            for lineno, ln in enumerate(lines, start=1):
                m = re.match(r'^\s{0,4}"([^"]+)"\s*:', ln)
                if m and m.group(1) not in key_lines:
                    key_lines[m.group(1)] = lineno
            rows: list[str] = []
            for k, v in list(parsed.items())[:_MAX_OUTLINE_LINES]:
                ln_tag = f"L{key_lines[k]}: " if k in key_lines else ""
                if isinstance(v, list):
                    desc = f"array[{len(v)}]"
                elif isinstance(v, dict):
                    desc = f"object{{{len(v)} keys}}"
                else:
                    desc = repr(v)[:60]
                rows.append(f"{ln_tag}{k}: {desc}")
            if len(parsed) > _MAX_OUTLINE_LINES:
                rows.append(f"... (+{len(parsed) - _MAX_OUTLINE_LINES} more keys)")
            return "\n".join(rows)
        if isinstance(parsed, list) and len(parsed) >= 5:
            sample = json.dumps(parsed[0], separators=(",", ":"))[:140]
            return f"JSON array[{len(parsed)} items]\nFirst item: {sample}"

    # Large YAML / TOML -> top-level sections/keys with exact line numbers
    if ext in (".yaml", ".yml"):
        rows = []
        for lineno, ln in enumerate(lines, start=1):
            m = _YAML_TOP_RE.match(ln)
            if m:
                val = m.group(2).strip()[:60]
                rows.append(f"L{lineno}: {m.group(1)}: {val}" if val else f"L{lineno}: {m.group(1)}:")
                if len(rows) >= _MAX_OUTLINE_LINES:
                    rows.append("... (more keys follow)")
                    break
        if len(rows) >= 3:
            return "\n".join(rows)

    if ext == ".toml":
        rows = []
        for lineno, ln in enumerate(lines, start=1):
            s = ln.strip()
            if _TOML_SEC_RE.match(s):
                rows.append(f"L{lineno}: {s[:90]}")
                if len(rows) >= _MAX_OUTLINE_LINES:
                    rows.append("... (more sections follow)")
                    break
        if len(rows) >= 2:
            return "\n".join(rows)

    # Large logs / NDJSON / CSV -> auto-index into SQLite FTS5 sandbox + return head/signals/tail
    if ext in _LOG_EXTS:
        aid: str | None = None
        try:
            from runtime import context_sandbox

            aid = context_sandbox.store_output(text, command=f"read:{p.name}", env=env)
        except Exception:
            aid = None
        parts: list[str] = [f"Header ({len(lines):,} lines total):"]
        for i, ln in enumerate(lines[:5], start=1):
            parts.append(f"L{i}: {ln[:140]}")
        sig_lines = [
            f"L{i}: {ln[:140]}"
            for i, ln in enumerate(lines[5:-5], start=6)
            if _LOG_SIGNAL_RE.search(ln)
        ]
        if sig_lines:
            parts.append(f"Signal lines ({len(sig_lines)} matches, showing up to 12):")
            parts.extend(sig_lines[:12])
        if len(lines) > 10:
            parts.append("Tail:")
            for i, ln in enumerate(lines[-5:], start=len(lines) - 4):
                parts.append(f"L{i}: {ln[:140]}")
        if aid:
            from runtime import context_sandbox

            parts.append(f"Indexed in sandbox: {context_sandbox.retrieval_hint(aid, env=env)}")
        return "\n".join(parts)

    return None


def build_code_outline(file_path: str, text: str, *, env: dict[str, str] | None = None) -> str | None:
    """Build a compact line-numbered symbol, doc-heading, or structured-data outline for a large file.

    Prefers ``runtime.code_outline`` for non-Python code, falling back to
    Python's ``ast`` module (for .py), Markdown heading TOC (for .md), structured data
    outlines (JSON/YAML/TOML/lockfiles/logs), or a fast regex extractor.
    """
    ext = Path(file_path).suffix.lower()

    # 0. Large Markdown doc -> exact line-numbered heading table of contents
    if ext in _DOC_EXTS:
        rows: list[str] = []
        in_fence = False
        for lineno, line in enumerate(text.splitlines(), start=1):
            s = line.strip()
            if s.startswith("```") or s.startswith("~~~"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            m = _MD_HEADING_RE.match(s)
            if m:
                rows.append(f"L{lineno}: {m.group(1)} {m.group(2)[:90]}")
                if len(rows) >= _MAX_OUTLINE_LINES:
                    rows.append("... (more headings follow)")
                    break
        return "\n".join(rows) if len(rows) >= 3 else None

    # 0b. Tier 2c: Lockfiles, JSON/YAML/TOML configs, and large logs/CSV/NDJSON
    if ext in _DATA_EXTS or ext in _LOG_EXTS or _LOCKFILE_NAME_RE.search(Path(file_path).name):
        return _outline_structured_data(file_path, text, env=env)

    if ext not in _CODE_EXTS:
        return None

    # 1. Native multi-language outline (Python uses the exact AST path below)
    if ext != ".py":
        try:
            from runtime import code_outline

            summary = code_outline.outline(text, file_path)
            if summary and len(summary) < int(len(text) * 0.35):
                return summary
        except Exception:
            pass

    # 2. Fast exact Python AST outline
    if ext == ".py":
        try:
            tree = ast.parse(text)
            rows: list[str] = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                    args = [a.arg for a in node.args.args]
                    end_ln = getattr(node, "end_lineno", node.lineno)
                    rows.append(f"L{node.lineno}-{end_ln}: {prefix} {node.name}({', '.join(args[:6])})")
                elif isinstance(node, ast.ClassDef):
                    end_ln = getattr(node, "end_lineno", node.lineno)
                    rows.append(f"L{node.lineno}-{end_ln}: class {node.name}")
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            m_end = getattr(item, "end_lineno", item.lineno)
                            rows.append(f"  L{item.lineno}-{m_end}: def {item.name}(...)")
            if rows:
                if len(rows) > _MAX_OUTLINE_LINES:
                    rows = rows[:_MAX_OUTLINE_LINES] + [f"... (+{len(rows) - _MAX_OUTLINE_LINES} more symbols)"]
                return "\n".join(rows)
        except SyntaxError:
            pass

    # 3. Generic multi-language regex outline with exact block end lines
    try:
        from runtime.index_mcp import _find_block_end_line
    except Exception:
        _find_block_end_line = None  # type: ignore

    lines = text.splitlines()
    rows = []
    for lineno, line in enumerate(lines, start=1):
        if _GENERIC_SYM_RE.match(line):
            end_ln = _find_block_end_line(lines, lineno - 1, ext) if _find_block_end_line else lineno
            span = f"L{lineno}-{end_ln}" if end_ln > lineno else f"L{lineno}"
            rows.append(f"{span}: {line.strip()[:100]}")
            if len(rows) >= _MAX_OUTLINE_LINES:
                rows.append("... (more symbols follow)")
                break
    if len(rows) >= 3:
        return "\n".join(rows)
    return None


_RETRY_WINDOW_S = 180.0


def _note_retry(prev: dict[str, Any], soft_denies: int, host: str, env: dict[str, str] | None) -> None:
    """Record that the model re-requested content right after a short funnel answer."""
    tier = prev.pop("last_tier", None)
    try:
        recent = (time.time() - float(prev.get("ts") or 0.0)) <= _RETRY_WINDOW_S
    except (TypeError, ValueError):
        recent = False
    if tier and soft_denies >= _SOFT_DENY_CAP and recent:
        record_funnel_event(str(tier), "retry", host=host, env=env)


_SYMBOL_DELTA_MAX_REGIONS = 4
_SYMBOL_DELTA_MAX_RATIO = 0.40


def _symbol_region_delta(file_path: str, old_text: str, new_text: str) -> str | None:
    """Describe a change as whole replaced symbol regions when every hunk sits inside a symbol.

    Lossless by construction: each region is bounded by unchanged anchor lines (or the file
    edges), so `old lines a-b` -> `current lines c-d` plus the unchanged rest is the full file.
    """
    if Path(file_path).suffix.lower() not in _CODE_EXTS:
        return None
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    if not new_lines or not old_lines:
        return None
    ops = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
    hunks = [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in ops if tag != "equal"]
    if not hunks:
        return None
    try:
        from runtime import index_mcp

        _, syms, _ = index_mcp._source_snapshot(Path(file_path).name, new_text.encode("utf-8"))
    except Exception:
        return None
    spans = sorted(
        {(int(x["start_line"]), int(x["end_line"]), str(x.get("qualified_name") or x.get("name") or "")) for x in syms
         if int(x.get("end_line") or 0) >= int(x.get("start_line") or 0) >= 1},
        key=lambda t: (t[1] - t[0], t[0]),
    )
    if not spans:
        return None
    n_new = len(new_lines)
    n_old = len(old_lines)

    def new_to_old(line: int) -> int | None:
        for tag, i1, _i2, j1, j2 in ops:
            if tag == "equal" and j1 < line <= j2:
                return i1 + (line - j1)
        return None

    regions: list[list[Any]] = []
    for i1, i2, j1, j2 in hunks:
        if j2 > j1:
            lo, hi = j1 + 1, j2
        else:  # pure deletion between new lines j1 and j1+1
            lo, hi = j1, j1 + 1
        owner = next((sp for sp in spans if sp[0] <= lo and sp[1] >= hi), None)
        if owner is None:
            return None
        a, b = owner[0], owner[1]
        steps = 0
        while a > 1 and new_to_old(a - 1) is None and steps < 6:
            a -= 1
            steps += 1
        steps = 0
        while b < n_new and new_to_old(b + 1) is None and steps < 6:
            b += 1
            steps += 1
        if (a > 1 and new_to_old(a - 1) is None) or (b < n_new and new_to_old(b + 1) is None):
            return None
        regions.append([a, b, {owner[2]}])
    regions.sort(key=lambda r: r[0])
    merged: list[list[Any]] = []
    for r in regions:
        if merged and r[0] <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], r[1])
            merged[-1][2] |= r[2]
        else:
            merged.append(r)
    if len(merged) > _SYMBOL_DELTA_MAX_REGIONS:
        return None
    shown = sum(b - a + 1 for a, b, _ in merged)
    if shown > n_new * _SYMBOL_DELTA_MAX_RATIO:
        return None
    blocks: list[str] = []
    for a, b, names in merged:
        before = new_to_old(a - 1) if a > 1 else 0
        after = new_to_old(b + 1) if b < n_new else n_old + 1
        if before is None or after is None:
            return None
        oa, ob = before + 1, after - 1
        old_rng = f"old L{oa}-L{ob}" if ob >= oa else f"insert after old L{before}"
        label = ", ".join(sorted(n for n in names if n)) or "symbol"
        body = "\n".join(f"{ln}: {new_lines[ln - 1]}" for ln in range(a, b + 1))
        blocks.append(f"{old_rng} -> now L{a}-L{b} (`{label}`):\n{body}")
    out = "\n".join(blocks)
    if len(out) > len(new_text) * _SYMBOL_DELTA_MAX_RATIO:
        return None
    return out


def check_read(
    session_id: str,
    file_path: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    byte_offset: int | None = None,
    cfg: dict[str, Any],
    host: str = "antigravity",
    now: float | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Evaluate a file read through the 4-tier funnel.

    Returns ``{"action": "deny", "reason": "..."}`` if intercepted (with bounded 1-retry escape),
    or ``None`` to allow the read untouched.
    """
    if not cfg.get("read_cache", True):
        return None
    if not session_id or not _SID_RE.match(session_id):
        return None
    if byte_offset not in (None, 0):
        return None
    if not file_path:
        return None

    target = Path(file_path)
    try:
        st = target.stat()
    except OSError:
        return None
    if not target.is_file() or st.st_size > _MAX_FILE_BYTES or st.st_size == 0:
        return None

    now = time.time() if now is None else float(now)
    key = _norm_path(file_path)
    basename = target.name
    store = _load_store(session_id, env)
    reads = store["reads"]
    prev = reads.get(key)

    # Stat fastpath: if (mtime_ns, size) matches a previously cached non-outline read
    # covering the requested span, serve T0 immediately without reading or hashing the file.
    if (
        isinstance(prev, dict)
        and not prev.get("outline_only")
        and prev.get("sha")
        and prev.get("mtime_ns") == st.st_mtime_ns
        and prev.get("size") == st.st_size
        and _intervals_cover(prev, start_line, end_line)
    ):
        soft_denies = int(prev.get("soft_denies") or 0)
        if soft_denies < _SOFT_DENY_CAP and not cfg.get("shadow") and not tier_backed_off("t0", host, env):
            prev["soft_denies"] = soft_denies + 1
            prev["ts"] = now
            prev["last_tier"] = "t0"
            _save_store(session_id, store, now, env)
            rng = _format_range(start_line, end_line)
            reason = (
                f"[Terser] `{basename}` ({rng}) is unchanged since your earlier read. "
                "Reuse the content already in context, or retry to refresh (e.g. after /compact)."
            )
            record_savings("read_dedup", st.st_size, len(reason), host=host, detail=basename, env=env)
            record_funnel_event("t0", "served", host=host, env=env)
            return {"action": "deny", "reason": reason}
        _note_retry(prev, soft_denies, host, env)
        prev["soft_denies"] = 0
        prev["ts"] = now
        _save_store(session_id, store, now, env)
        return None

    try:
        raw_bytes = target.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw_bytes[:4096]:
        return None

    sha = hashlib.sha256(raw_bytes).hexdigest()

    text_snapshot: str | None = (
        raw_bytes.decode("utf-8", errors="replace")
        if len(raw_bytes) <= _MAX_SNAPSHOT_BYTES
        else None
    )

    if isinstance(prev, dict):
        prev_sha = prev.get("sha")
        prev_start = prev.get("start")
        prev_end = prev.get("end")
        soft_denies = int(prev.get("soft_denies") or 0)
        outline_only = bool(prev.get("outline_only"))

        # If previous call only returned an outline, the immediate retry loads the full file!
        # Also track full-file overrides: if the agent overrides outlines repeatedly in a
        # session (task genuinely requires whole-file reads), auto-backoff outline_first
        # to avoid extra round-trip turns (round-trip cost guardrail).
        if outline_only:
            _note_retry(prev, soft_denies, host, env)
            prev["outline_only"] = False
            prev["sha"] = sha
            prev["mtime_ns"] = st.st_mtime_ns
            prev["size"] = st.st_size
            prev["start"] = start_line
            prev["end"] = end_line
            prev["intervals"] = _merge_intervals(None, start_line, end_line)
            prev["soft_denies"] = 0
            prev["ts"] = now
            if text_snapshot is not None:
                prev["snapshot"] = text_snapshot
            total_lines_retry = raw_bytes.count(b"\n") + 1
            rs_r = max(1, start_line) if start_line is not None else 1
            re_r = min(total_lines_retry, end_line) if end_line is not None else total_lines_retry
            if (start_line is None and end_line is None) or (re_r - rs_r + 1) >= _LARGE_SLICE_MIN_LINES:
                meta = store.setdefault("meta", {})
                meta["outline_overrides"] = int(meta.get("outline_overrides") or 0) + 1
            _save_store(session_id, store, now, env)
            return None

        # Tier 0: Unchanged re-read (full coverage OR high partial-overlap trimming)
        if prev_sha == sha and _intervals_cover(prev, start_line, end_line):
            if soft_denies < _SOFT_DENY_CAP and not cfg.get("shadow") and not tier_backed_off("t0", host, env):
                prev["soft_denies"] = soft_denies + 1
                prev["ts"] = now
                prev["last_tier"] = "t0"
                _save_store(session_id, store, now, env)
                rng = _format_range(start_line, end_line)
                reason = (
                    f"[Terser] `{basename}` ({rng}) is unchanged since your earlier read. "
                    "Reuse the content already in context, or retry to refresh (e.g. after /compact)."
                )
                record_savings("read_dedup", len(raw_bytes), len(reason), host=host, detail=basename, env=env)
                record_funnel_event("t0", "served", host=host, env=env)
                return {"action": "deny", "reason": reason}
            _note_retry(prev, soft_denies, host, env)
            prev["soft_denies"] = 0
            prev["ts"] = now
            _save_store(session_id, store, now, env)
            return None

        # Tier 0b: Partial-overlap range trimming — if >=40 lines & >=40% of requested span is already loaded
        if (
            prev_sha == sha
            and soft_denies < _SOFT_DENY_CAP
            and not cfg.get("shadow")
            and (start_line is not None or end_line is not None)
            and not tier_backed_off("t0b", host, env)
        ):
            total_lines = raw_bytes.count(b"\n") + 1
            rs = max(1, start_line) if start_line is not None else 1
            re_ = min(total_lines, end_line) if end_line is not None else total_lines
            req_span = max(1, re_ - rs + 1)
            prev_ivs = prev.get("intervals") if isinstance(prev.get("intervals"), list) and prev.get("intervals") else [[prev.get("start") or 1, prev.get("end")]]
            uncovered, cov_lines = _uncovered_subranges(prev_ivs, start_line, end_line, total_lines)
            if uncovered and cov_lines >= 40 and (cov_lines / req_span) >= 0.40:
                prev["soft_denies"] = soft_denies + 1
                prev["ts"] = now
                prev["last_tier"] = "t0b"
                record_funnel_event("t0b", "served", host=host, env=env)
                _save_store(session_id, store, now, env)
                spans_str = ", ".join(f"L{s}-L{e}" for s, e in uncovered[:4])
                range_hint = "StartLine/EndLine" if host == "antigravity" else "offset/limit"
                reason = (
                    f"[Terser] `{basename}`: {cov_lines}/{req_span} requested lines are already in context. "
                    f"Read only the unread span ({spans_str}) using {range_hint}, or retry to load the full range."
                )
                est_saved_bytes = int(len(raw_bytes) * (cov_lines / max(total_lines, 1)))
                record_savings("read_trim", max(len(reason) + 1, est_saved_bytes), len(reason), host=host, detail=basename, env=env)
                return {"action": "deny", "reason": reason}

        # Tier 1: Compact unified delta diff on small edits
        old_snapshot = (
            _snapshot_text(prev, env)
            if (
                cfg.get("delta_diff", True)
                and prev_sha != sha
                and soft_denies < _SOFT_DENY_CAP
                and not cfg.get("shadow")
                and text_snapshot is not None
                and _intervals_cover(prev, start_line, end_line)
            )
            else None
        )
        if old_snapshot is not None and text_snapshot is not None:
            diff = (
                None if tier_backed_off("t1", host, env)
                else _compact_unified_diff(old_snapshot, text_snapshot, basename, start_line, end_line)
            )
            reason = ""
            tier = ""
            if diff:
                reason = (
                    f"[Terser] `{basename}` changed slightly since your last read. "
                    f"Exact unified diff:\n```diff\n{diff}\n```\n(Retry read to load the full file.)"
                )
                tier = "t1"
            elif (
                cfg.get("symbol_delta", True)
                and not tier_backed_off("t1sym", host, env)
                and start_line is None
                and end_line is None
                and _intervals_cover(prev, None, None)
            ):
                regions = _symbol_region_delta(file_path, old_snapshot, text_snapshot)
                if regions:
                    reason = (
                        f"[Terser] `{basename}` changed only inside the symbol(s) below since your last read; "
                        "every other line is unchanged. Each block replaces the old line range shown, "
                        f"with current line numbers:\n{regions}\n(Retry read to load the full file.)"
                    )
                    tier = "t1sym"
            if reason:
                prev["sha"] = sha
                prev["mtime_ns"] = st.st_mtime_ns
                prev["size"] = st.st_size
                prev["snapshot"] = text_snapshot
                prev["soft_denies"] = soft_denies + 1
                prev["ts"] = now
                prev["last_tier"] = tier
                _save_store(session_id, store, now, env)
                record_savings(
                    "read_delta" if tier == "t1" else "read_symbol_delta",
                    len(raw_bytes), len(reason), host=host, detail=basename, env=env,
                )
                record_funnel_event(tier, "served", host=host, env=env)
                return {"action": "deny", "reason": reason}

    # Tier 2: First unranged read (or huge first-time ranged slice >= 400L) of a LARGE code/doc/data file
    # -> inline line-numbered outline ONCE (auto-backs off after `outline_backoff_limit` overrides).
    backoff_limit = int(cfg.get("outline_backoff_limit", 2))
    outline_overrides = int((store.get("meta") or {}).get("outline_overrides") or 0)
    is_lockfile = bool(_LOCKFILE_NAME_RE.search(basename))
    is_unranged = start_line is None and end_line is None
    outline_min_bytes = int(cfg.get("outline_min_bytes", 10240))
    if (
        prev is None
        and cfg.get("outline_first", True)
        and not tier_backed_off("t2", host, env)
        and (backoff_limit <= 0 or outline_overrides < backoff_limit)
        and not cfg.get("shadow")
        and st.st_size >= (outline_min_bytes if is_unranged else max(_LARGE_SLICE_MIN_BYTES, outline_min_bytes))
    ):
        text_for_outline = text_snapshot or raw_bytes.decode("utf-8", errors="replace")
        total_lines = text_for_outline.count("\n") + 1
        rs_t2 = max(1, start_line) if start_line is not None else 1
        re_t2 = min(total_lines, end_line) if end_line is not None else total_lines
        req_span_t2 = max(0, re_t2 - rs_t2 + 1)
        min_lines = 1 if is_lockfile else int(cfg.get("outline_min_lines", 250))
        qualifies_t2 = (
            (is_unranged and total_lines >= min_lines)
            or (not is_unranged and req_span_t2 >= _LARGE_SLICE_MIN_LINES and total_lines >= min_lines)
        )
        if qualifies_t2:
            outline = _load_cached_outline(sha, env=env)
            if not outline:
                outline = build_code_outline(file_path, text_for_outline, env=env)
                if outline:
                    _save_cached_outline(sha, outline, env=env)
            if outline:
                reads[key] = {
                    "sha": sha,
                    "start": None,
                    "end": None,
                    "intervals": [],
                    "soft_denies": 1,
                    "outline_only": True,
                    "last_tier": "t2",
                    "ts": now,
                }
                record_funnel_event("t2", "served", host=host, env=env)
                _save_store(session_id, store, now, env)
                range_hint = "StartLine/EndLine" if host == "antigravity" else "offset/limit"
                slice_note = "" if is_unranged else f" (requested {req_span_t2} lines: L{rs_t2}-L{re_t2})"
                reason = (
                    f"[Terser] `{basename}` is {total_lines} lines ({st.st_size:,} B){slice_note}. "
                    f"Outline (exact line spans):\n{outline}\n\n"
                    f"Read only the needed span with {range_hint} (or `terser-index` `get_symbol_source`), "
                    "or retry this read immediately to load the full requested span."
                )
                record_savings("read_outline", len(raw_bytes), len(reason), host=host, detail=basename, env=env)
                return {"action": "deny", "reason": reason}

    # Tier 3: First read / wider range / large change -> record snapshot & disjoint intervals and allow
    if isinstance(prev, dict):
        _note_retry(prev, int(prev.get("soft_denies") or 0), host, env)
    existing_ivs = None
    if isinstance(prev, dict) and prev.get("sha") == sha:
        if isinstance(prev.get("intervals"), list) and prev["intervals"]:
            existing_ivs = prev["intervals"]
        else:
            existing_ivs = [[prev.get("start") or 1, prev.get("end")]]
    merged_ivs = _merge_intervals(existing_ivs, start_line, end_line)
    if len(merged_ivs) == 1:
        legacy_start = None if (merged_ivs[0][0] == 1 and merged_ivs[0][1] is None) else merged_ivs[0][0]
        legacy_end = merged_ivs[0][1]
    else:
        legacy_start, legacy_end = start_line, end_line

    entry: dict[str, Any] = {
        "sha": sha,
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "start": legacy_start,
        "end": legacy_end,
        "intervals": merged_ivs,
        "soft_denies": 0,
        "outline_only": False,
        "ts": now,
    }
    if text_snapshot is not None:
        entry["snapshot"] = text_snapshot
    reads[key] = entry
    _save_store(session_id, store, now, env)
    return None


def seed_after_write(
    session_id: str,
    file_path: str,
    *,
    written: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Seed the writing session's read cache after a full-file Write so immediate verification re-reads hit T0.

    Also invalidates sibling/parent subagent caches so they never serve stale T0 hits.
    """
    if not session_id or not file_path or not _SID_RE.match(session_id):
        return
    invalidate_on_edit(session_id, file_path, env=env)
    target = Path(file_path)
    try:
        st = target.stat()
        if not target.is_file() or st.st_size == 0 or st.st_size > _MAX_FILE_BYTES:
            return
        raw_bytes = target.read_bytes()
    except OSError:
        return
    if b"\x00" in raw_bytes[:4096]:
        return
    now = time.time()
    sha = hashlib.sha256(raw_bytes).hexdigest()
    key = _norm_path(file_path)
    store = _load_store(session_id, env)
    actual_text = raw_bytes.decode("utf-8", errors="replace")
    entry: dict[str, Any] = {
        "sha": sha,
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "start": None,
        "end": None,
        "intervals": [[1, None]],
        "soft_denies": 0,
        "outline_only": False,
        "ts": now,
    }
    if isinstance(written, str) and not _same_text(written, actual_text):
        # Something (a formatter, another hook) changed the file after the write. Cache the
        # content the model actually wrote so the next read returns an exact diff to disk.
        record_funnel_event("write_seed", "mismatch", env=env)
        if len(written.encode("utf-8")) > _MAX_SNAPSHOT_BYTES:
            store["reads"].pop(key, None)
            _save_store(session_id, store, now, env)
            return
        entry["sha"] = hashlib.sha256(written.encode("utf-8")).hexdigest()
        entry.pop("mtime_ns", None)
        entry["snapshot"] = written
    elif len(raw_bytes) <= _MAX_SNAPSHOT_BYTES:
        entry["snapshot"] = actual_text
    store["reads"][key] = entry
    _save_store(session_id, store, now, env)


def seed_read_interval(
    session_id: str,
    file_path: str,
    start_line: int,
    end_line: int,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Seed an already-loaded line interval into read-cache (e.g. from terser-index `get_symbol_source`)."""
    if not session_id or not file_path or not _SID_RE.match(session_id):
        return
    if start_line < 1 or end_line < start_line:
        return
    target = Path(file_path)
    try:
        st = target.stat()
        if not target.is_file() or st.st_size == 0 or st.st_size > _MAX_FILE_BYTES:
            return
        raw_bytes = target.read_bytes()
    except OSError:
        return
    if b"\x00" in raw_bytes[:4096]:
        return
    now = time.time()
    sha = hashlib.sha256(raw_bytes).hexdigest()
    key = _norm_path(file_path)
    store = _load_store(session_id, env)
    prev = store["reads"].get(key)
    existing_ivs = None
    if isinstance(prev, dict) and prev.get("sha") == sha and not prev.get("outline_only"):
        if isinstance(prev.get("intervals"), list) and prev["intervals"]:
            existing_ivs = prev["intervals"]
        else:
            existing_ivs = [[prev.get("start") or 1, prev.get("end")]]
    merged_ivs = _merge_intervals(existing_ivs, start_line, end_line)
    entry: dict[str, Any] = {
        "sha": sha,
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "start": merged_ivs[0][0] if len(merged_ivs) == 1 else start_line,
        "end": merged_ivs[0][1] if len(merged_ivs) == 1 else end_line,
        "intervals": merged_ivs,
        "soft_denies": 0,
        "outline_only": False,
        "ts": now,
    }
    if len(raw_bytes) <= _MAX_SNAPSHOT_BYTES:
        entry["snapshot"] = raw_bytes.decode("utf-8", errors="replace")
    store["reads"][key] = entry
    _save_store(session_id, store, now, env)


def _same_text(expected: str, actual: str) -> bool:
    # Line-ending-only differences are invisible in Read output, so they count as equal.
    return expected == actual or expected.replace("\r\n", "\n") == actual.replace("\r\n", "\n")


EditSpec = tuple[str, str, bool]


def _apply_edits(snap: str, edits: list[EditSpec]) -> tuple[str, list[tuple[int, int, int]] | None] | None:
    """Apply edits like the host tool does. Returns new text plus per-edit (start, end, delta)
    line spans in running coordinates (None when replace_all makes spans ambiguous)."""
    cur = snap
    spans: list[tuple[int, int, int]] | None = []
    for old, new, replace_all in edits:
        if not old:
            return None
        cnt = cur.count(old)
        if cnt == 0:
            return None
        if replace_all:
            cur = cur.replace(old, new)
            spans = None
            continue
        if cnt != 1:
            return None
        pos = cur.find(old)
        start_line = cur.count("\n", 0, pos) + 1
        if spans is not None:
            spans.append((start_line, start_line + old.count("\n"), new.count("\n") - old.count("\n")))
        cur = cur[:pos] + new + cur[pos + len(old):]
    return cur, spans


def _edit_variants(snap: str, edits: list[EditSpec]) -> list[list[EditSpec]]:
    out = [edits]
    if "\r\n" in snap:
        out.append([
            (o.replace("\r\n", "\n").replace("\n", "\r\n"), n.replace("\r\n", "\n").replace("\n", "\r\n"), ra)
            for o, n, ra in edits
        ])
    return out


def _shift_intervals(raw_ivs: list[Any], edit_start: int, edit_end: int, delta: int) -> list[list[int | None]]:
    shifted: list[list[int | None]] = []
    for iv in raw_ivs:
        if not isinstance(iv, list) or len(iv) != 2:
            continue
        s = int(iv[0]) if iv[0] is not None else 1
        e = int(iv[1]) if iv[1] is not None else None
        if e is None:
            new_s = max(1, s + delta) if s > edit_end else s
            shifted.append([new_s, None])
        elif e < edit_start:
            shifted.append([s, e])
        elif s > edit_end:
            shifted.append([max(1, s + delta), max(1, e + delta)])
        else:
            shifted.append([s, max(s, e + delta)])
    return shifted


def seed_after_edit(
    session_id: str,
    file_path: str,
    *,
    old_text: str | None = None,
    new_text: str | None = None,
    replace_all: bool = False,
    edits: list[EditSpec] | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Update the editing session's cached sha/snapshot after a successful Edit / MultiEdit.

    The cache is only advanced when the file on disk equals the cached snapshot with the
    edits applied, so a formatter or concurrent writer can never produce a false "unchanged".
    On mismatch the cache holds the model's expected text, so the next read is an exact diff.
    Partial-coverage seeding additionally requires each edit inside an already-read span.
    Sibling/parent subagents are always invalidated.
    """
    if not session_id or not file_path or not _SID_RE.match(session_id):
        return
    invalidate_on_edit(session_id, file_path, env=env)
    key = _norm_path(file_path)
    store = _load_store(session_id, env)
    prev = store["reads"].get(key)
    if not isinstance(prev, dict) or prev.get("outline_only"):
        return
    if edits is None:
        edits = [(old_text, new_text, bool(replace_all))] if isinstance(old_text, str) and isinstance(new_text, str) else []
    edits = [(o, n, bool(ra)) for o, n, ra in edits if isinstance(o, str) and isinstance(n, str)]

    target = Path(file_path)
    try:
        st = target.stat()
        if not target.is_file() or st.st_size == 0 or st.st_size > _MAX_FILE_BYTES:
            return
        raw_bytes = target.read_bytes()
    except OSError:
        return
    if b"\x00" in raw_bytes[:4096]:
        return
    now = time.time()
    actual = raw_bytes.decode("utf-8", errors="replace") if len(raw_bytes) <= _MAX_SNAPSHOT_BYTES else None
    full_covered = _intervals_cover(prev, None, None)
    snap = _snapshot_text(prev, env)

    if isinstance(snap, str) and edits:
        expected: tuple[str, list[tuple[int, int, int]] | None] | None = None
        for variant in _edit_variants(snap, edits):
            expected = _apply_edits(snap, variant)
            if expected is not None and actual is not None and _same_text(expected[0], actual):
                break
        else:
            # Disk differs from what the model expects (formatter, external write, or an edit
            # the cache can't model). Keep the model's view so the next read is an exact diff.
            record_funnel_event("edit_seed", "mismatch", env=env)
            if expected is not None and full_covered and len(expected[0].encode("utf-8")) <= _MAX_SNAPSHOT_BYTES:
                prev["sha"] = hashlib.sha256(expected[0].encode("utf-8")).hexdigest()
                prev["snapshot"] = expected[0]
                prev.pop("mtime_ns", None)
                prev["soft_denies"] = 0
                prev["ts"] = now
                store["reads"][key] = prev
                _save_store(session_id, store, now, env)
            return
        assert expected is not None
        if not full_covered:
            spans = expected[1]
            if not spans:
                return
            ivs: list[Any] = (
                prev.get("intervals")
                if isinstance(prev.get("intervals"), list) and prev.get("intervals")
                else [[prev.get("start") or 1, prev.get("end")]]
            )
            probe = dict(prev)
            for edit_start, edit_end, delta in spans:
                probe["intervals"] = ivs
                if not _intervals_cover(probe, edit_start, edit_end):
                    return
                if delta:
                    ivs = _shift_intervals(ivs, edit_start, edit_end, delta)
                    if not ivs:
                        return
            prev["intervals"] = ivs
            if len(ivs) == 1:
                prev["start"] = None if (ivs[0][0] == 1 and ivs[0][1] is None) else ivs[0][0]
                prev["end"] = ivs[0][1]
    else:
        # No snapshot to replay against (large file) or unknown edit payload: only seed a
        # fully-read file when the byte-size change matches the edits exactly.
        if not full_covered or not edits or any(ra for _, _, ra in edits):
            return
        prev_size = prev.get("size")
        if not isinstance(prev_size, int):
            return
        lf_delta = sum(len(n.encode("utf-8")) - len(o.encode("utf-8")) for o, n, _ in edits)
        crlf_delta = lf_delta + sum(n.count("\n") - o.count("\n") for o, n, _ in edits)
        if st.st_size - prev_size not in (lf_delta, crlf_delta):
            return
        if any(n and n.replace("\r\n", "\n").encode("utf-8") not in raw_bytes.replace(b"\r\n", b"\n") for _, n, _ in edits):
            return

    prev["sha"] = hashlib.sha256(raw_bytes).hexdigest()
    prev["mtime_ns"] = st.st_mtime_ns
    prev["size"] = st.st_size
    prev["soft_denies"] = 0
    prev["outline_only"] = False
    prev["ts"] = now
    if actual is not None:
        prev["snapshot"] = actual
    else:
        _drop_snapshot(prev)
    store["reads"][key] = prev
    _save_store(session_id, store, now, env)


def invalidate_on_edit(
    session_id: str,
    file_path: str,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Reset soft_denies after an edit while keeping the prior snapshot so the next read gets a delta diff.

    Propagates invalidation across parent and sibling subagent caches sharing the same base session.
    """
    if not session_id or not file_path or not _SID_RE.match(session_id):
        return
    key = _norm_path(file_path)
    now = time.time()

    target_sids: list[str] = [session_id]
    base_sid = session_id.split("--", 1)[0]
    if base_sid and base_sid != session_id:
        target_sids.append(base_sid)

    cdir = _cache_dir(env)
    safe_base = re.sub(r"[^0-9a-zA-Z_.-]", "_", base_sid)[:44]
    if safe_base and cdir.is_dir():
        try:
            for p in cdir.glob(f"{safe_base}--*.json"):
                stem = p.stem
                if stem not in target_sids:
                    target_sids.append(stem)
        except OSError:
            pass

    for sid in target_sids:
        store = _load_store(sid, env)
        changed = False
        entry = store["reads"].get(key)
        if isinstance(entry, dict):
            entry["soft_denies"] = 0
            entry["outline_only"] = False
            entry.pop("mtime_ns", None)
            changed = True
        # Any MCP server (code indexes, docs-from-repo, DB views) may read the edited file.
        if isinstance(store.get("mcp"), dict) and store["mcp"]:
            store["mcp"] = {}
            changed = True
        if changed:
            _save_store(sid, store, now, env)


def _split_mcp_name(name: str) -> tuple[str, str]:
    """``(server, tool)`` for ``server:tool``, ``mcp__server__tool`` or a bare tool name."""
    server = ""
    tool_part = name
    if ":" in tool_part:
        server, tool_part = tool_part.rsplit(":", 1)
    if "__" in tool_part:
        head, tool_part = tool_part.rsplit("__", 1)
        server = server or head
    if server.startswith("mcp__"):
        server = server[5:]
    return server, tool_part


def _is_mutating_or_live_mcp(name: str) -> bool:
    # Tool tokens decide on their own; server tokens only mark live-state servers, so server
    # names like `terser-index` never misclassify read-only tools such as `search_symbols`.
    server, tool_part = _split_mcp_name(name)
    tokens = [t for t in re.split(r"[^a-z0-9]+", tool_part.lower()) if t]
    if any(t in _MUTATING_OR_LIVE_MCP_TOKENS for t in tokens):
        return True
    server_tokens = [t for t in re.split(r"[^a-z0-9]+", server.lower()) if t]
    return any(t in _LIVE_MCP_SERVER_TOKENS for t in server_tokens)


def check_mcp_refetch(
    session_id: str,
    display_name: str,
    inner_args: Any,
    *,
    cfg: dict[str, Any],
    host: str = "antigravity",
    now: float | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Bounded duplicate-call guard for identical read-only MCP calls within 120s."""
    if host == "codex" or not cfg.get("mcp_refetch_guard", True) or cfg.get("shadow"):
        return None
    if not session_id or not _SID_RE.match(session_id):
        return None
    if not display_name:
        return None
    if _is_mutating_or_live_mcp(display_name):
        # A mutating or live call can change what any earlier read-only call would return.
        store = _load_store(session_id, env)
        if store.get("mcp"):
            store["mcp"] = {}
            _save_store(session_id, store, time.time() if now is None else float(now), env)
        return None

    now = time.time() if now is None else float(now)
    window = float(cfg.get("mcp_dedup_window_s", 120))
    try:
        canonical = json.dumps({"tool": display_name, "args": inner_args}, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    fp = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    store = _load_store(session_id, env)
    mcp = store["mcp"]
    prev = mcp.get(fp)
    if isinstance(prev, dict):
        elapsed = now - float(prev.get("ts") or 0.0)
        soft_denies = int(prev.get("soft_denies") or 0)
        if 0.0 <= elapsed <= window and soft_denies < _SOFT_DENY_CAP:
            prev["soft_denies"] = soft_denies + 1
            _save_store(session_id, store, now, env)
            reason = (
                f"[Terser] Identical read-only MCP call `{display_name}` ran {max(1, int(elapsed))}s ago. "
                "Reuse the earlier result, or retry if live state changed."
            )
            raw_est = max(len(reason) + 1, int(prev.get("resp_bytes") or 2000))
            record_savings("mcp_dedup", raw_est, len(reason), host=host, detail=display_name, env=env)
            return {"action": "deny", "reason": reason}

    mcp[fp] = {"tool": display_name, "ts": now, "soft_denies": 0}
    _save_store(session_id, store, now, env)
    return None


def record_mcp_response_size(
    session_id: str,
    display_name: str,
    inner_args: Any,
    resp_bytes: int,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Record actual MCP response byte length for accurate mcp_dedup savings telemetry."""
    if not session_id or not _SID_RE.match(session_id) or not display_name or resp_bytes <= 0:
        return
    try:
        canonical = json.dumps({"tool": display_name, "args": inner_args}, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    fp = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    store = _load_store(session_id, env)
    prev = store["mcp"].get(fp)
    if isinstance(prev, dict):
        prev["resp_bytes"] = int(resp_bytes)
        _save_store(session_id, store, time.time(), env)


def check_search_repeat(
    session_id: str,
    tool_name: str,
    tool_input: Any,
    output: str,
    *,
    basis: str | None = None,
    cwd: str = "",
    host: str = "claude",
    env: dict[str, str] | None = None,
) -> str | None:
    """Elide a Glob/Grep result identical to one already returned this session (once).

    Returns the replacement text, or None to pass the output through. An immediate
    identical re-run always gets the full output, and /compact clears this state.
    """
    if not session_id or not _SID_RE.match(session_id) or len(output) <= 600:
        return None
    try:
        canonical = json.dumps({"t": tool_name, "a": tool_input, "cwd": cwd}, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    fp = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    # `basis` is the result without volatile fields (e.g. `durationMs`), so timing alone
    # never makes two identical results look different.
    out_hash = hashlib.sha256((basis if basis is not None else output).encode("utf-8", errors="replace")).hexdigest()[:32]
    now = time.time()
    store = _load_store(session_id, env)
    searches = store["searches"]
    prev = searches.get(fp)
    if isinstance(prev, dict) and prev.get("h") == out_hash:
        if int(prev.get("soft") or 0) < _SOFT_DENY_CAP:
            prev["soft"] = int(prev.get("soft") or 0) + 1
            prev["ts"] = now
            _save_store(session_id, store, now, env)
            lines = output.count("\n") + 1
            msg = (
                f"[Terser] Same {tool_name} arguments returned the identical result ({lines} lines) "
                "earlier in this session. Reuse that result, or run it again to get the full output."
            )
            record_savings("search_repeat", len(output), len(msg), host=host, detail=tool_name, env=env)
            record_funnel_event("search_repeat", "served", host=host, env=env)
            return msg
        record_funnel_event("search_repeat", "retry", host=host, env=env)
    searches[fp] = {"h": out_hash, "soft": 0, "ts": now}
    _save_store(session_id, store, now, env)
    return None
