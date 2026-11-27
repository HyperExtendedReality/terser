"""Terser — 4-Tier Quality-Preserving Read Funnel & MCP Refetch Guard.

Unifies Terser's Read Cache + Delta Diff + Structure Map with symbol-first
AST navigation into one conflict-free PreToolUse funnel:

  Tier 0 (Unchanged re-read):
      Soft-deny ONCE with a concise pointer to the already-loaded context.
      Immediate retry always passes through (safe across /compact).
  Tier 1 (Small-edit re-read):
      Return exact unified diff (<= 60 lines & < 40% of slice) ONCE instead of
      re-reading the entire file. Immediate retry returns the full file.
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
})

# Regex fallback for non-Python code outlines when vendor structure_map isn't matched
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


def _sweep_stale(cdir: Path, now: float) -> None:
    try:
        if not cdir.is_dir():
            return
        cutoff = now - _STORE_TTL_SECONDS
        for p in cdir.glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _load_store(session_id: str, env: dict[str, str] | None = None) -> dict[str, dict]:
    p = _cache_path(session_id, env)
    try:
        if not p.is_file():
            return {"reads": {}, "mcp": {}}
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"reads": {}, "mcp": {}, "meta": {}}
        return {
            "reads": data.get("reads") if isinstance(data.get("reads"), dict) else {},
            "mcp": data.get("mcp") if isinstance(data.get("mcp"), dict) else {},
            "meta": data.get("meta") if isinstance(data.get("meta"), dict) else {},
        }
    except (OSError, json.JSONDecodeError, ValueError):
        return {"reads": {}, "mcp": {}, "meta": {}}


def _save_store(session_id: str, store: dict, now: float, env: dict[str, str] | None = None) -> None:
    cdir = _cache_dir(env)
    _sweep_stale(cdir, now)
    for bucket_key in ("reads", "mcp"):
        bucket = store.get(bucket_key)
        if isinstance(bucket, dict) and len(bucket) > _MAX_STORE_ENTRIES:
            ordered = sorted(
                bucket.items(),
                key=lambda kv: kv[1].get("ts", 0) if isinstance(kv[1], dict) else 0,
                reverse=True,
            )[:_MAX_STORE_ENTRIES]
            store[bucket_key] = dict(ordered)
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
    p_s = 1 if prev_start is None else prev_start
    r_s = 1 if req_start is None else req_start
    if r_s < p_s:
        return False
    if prev_end is None:
        return True
    if req_end is None:
        return False
    return req_end <= prev_end


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
    lines = text.splitlines(keepends=True)
    s = max(0, (start_line - 1) if start_line else 0)
    e = end_line if end_line else len(lines)
    return lines[s:e]


def _compact_unified_diff(
    old_text: str,
    new_text: str,
    basename: str,
    start_line: int | None,
    end_line: int | None,
) -> str | None:
    old_slice = _slice_lines(old_text, start_line, end_line)
    new_slice = _slice_lines(new_text, start_line, end_line)
    if not new_slice:
        return None
    diff_lines = list(
        difflib.unified_diff(
            old_slice,
            new_slice,
            fromfile=f"a/{basename}",
            tofile=f"b/{basename}",
            n=2,
        )
    )
    if not diff_lines or len(diff_lines) > _DELTA_MAX_DIFF_LINES:
        return None
    if len(new_slice) >= 20 and (len(diff_lines) / max(len(new_slice), 1)) > _DELTA_MAX_RATIO:
        return None
    return "".join(diff_lines).rstrip()


def _format_range(start_line: int | None, end_line: int | None) -> str:
    if start_line is None and end_line is None:
        return "full file"
    if start_line is not None and end_line is not None:
        return f"lines {start_line}-{end_line}"
    if start_line is not None:
        return f"from line {start_line}"
    return f"lines 1-{end_line}"


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
            parts.append(f'Indexed in sandbox: `terse query {aid} "<terms>"` or `terse expand {aid} <start> <end>`')
        return "\n".join(parts)

    return None


def build_code_outline(file_path: str, text: str, *, env: dict[str, str] | None = None) -> str | None:
    """Build a compact line-numbered symbol, doc-heading, or structured-data outline for a large file.

    Prefers Terser's bundled ``structure_map`` when available, falling back to
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

    # 1. Try Terser's bundled multi-language structure_map
    try:
        ensure_vendor_paths()
        import structure_map as _sm  # type: ignore

        if _sm.is_structure_supported_file(file_path):
            res = _sm.summarize_code_source(text, file_path)
            if res and getattr(res, "summary", None):
                summary = str(res.summary).strip()
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
        if soft_denies < _SOFT_DENY_CAP and not cfg.get("shadow"):
            prev["soft_denies"] = soft_denies + 1
            prev["ts"] = now
            _save_store(session_id, store, now, env)
            rng = _format_range(start_line, end_line)
            reason = (
                f"[Terser] `{basename}` ({rng}) is unchanged since your earlier read. "
                "Reuse the content already in context, or retry to refresh (e.g. after /compact)."
            )
            record_savings("read_dedup", st.st_size, len(reason), host=host, detail=basename, env=env)
            return {"action": "deny", "reason": reason}
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
            if soft_denies < _SOFT_DENY_CAP and not cfg.get("shadow"):
                prev["soft_denies"] = soft_denies + 1
                prev["ts"] = now
                _save_store(session_id, store, now, env)
                rng = _format_range(start_line, end_line)
                reason = (
                    f"[Terser] `{basename}` ({rng}) is unchanged since your earlier read. "
                    "Reuse the content already in context, or retry to refresh (e.g. after /compact)."
                )
                record_savings("read_dedup", len(raw_bytes), len(reason), host=host, detail=basename, env=env)
                return {"action": "deny", "reason": reason}
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
        if (
            cfg.get("delta_diff", True)
            and prev_sha != sha
            and soft_denies < _SOFT_DENY_CAP
            and not cfg.get("shadow")
            and isinstance(prev.get("snapshot"), str)
            and text_snapshot is not None
            and _intervals_cover(prev, start_line, end_line)
        ):
            diff = _compact_unified_diff(prev["snapshot"], text_snapshot, basename, start_line, end_line)
            if diff:
                prev["sha"] = sha
                prev["snapshot"] = text_snapshot
                prev["soft_denies"] = soft_denies + 1
                prev["ts"] = now
                _save_store(session_id, store, now, env)
                reason = (
                    f"[Terser] `{basename}` changed slightly since your last read. "
                    f"Exact unified diff:\n```diff\n{diff}\n```\n(Retry read to load the full file.)"
                )
                record_savings("read_delta", len(raw_bytes), len(reason), host=host, detail=basename, env=env)
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
                    "ts": now,
                }
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
    if len(raw_bytes) <= _MAX_SNAPSHOT_BYTES:
        entry["snapshot"] = raw_bytes.decode("utf-8", errors="replace")
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


def seed_after_edit(
    session_id: str,
    file_path: str,
    *,
    old_text: str | None = None,
    new_text: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Update the editing session's cached sha/snapshot after a successful partial Edit.

    Seeds when either:
      1. The session already loaded the FULL file, OR
      2. The edit is line-count-preserving (`delta_lines == 0`) inside an already-covered
         interval (so line numbers elsewhere never shift and unread lines are never masked).
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

    full_covered = _intervals_cover(prev, None, None)
    if not full_covered:
        # Check safe line-count-preserving partial-interval edit
        if (
            not isinstance(old_text, str)
            or not isinstance(new_text, str)
            or not old_text
            or old_text.count("\n") != new_text.count("\n")
        ):
            return
        snap = prev.get("snapshot")
        if not isinstance(snap, str) or snap.count(old_text) != 1:
            return
        pos = snap.find(old_text)
        if pos < 0:
            return
        edit_start = snap.count("\n", 0, pos) + 1
        edit_end = edit_start + old_text.count("\n")
        if not _intervals_cover(prev, edit_start, edit_end):
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
    prev["sha"] = hashlib.sha256(raw_bytes).hexdigest()
    prev["mtime_ns"] = st.st_mtime_ns
    prev["size"] = st.st_size
    prev["soft_denies"] = 0
    prev["outline_only"] = False
    prev["ts"] = now
    if len(raw_bytes) <= _MAX_SNAPSHOT_BYTES:
        prev["snapshot"] = raw_bytes.decode("utf-8", errors="replace")
    else:
        prev.pop("snapshot", None)
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
        mcp_map = store.get("mcp")
        if isinstance(mcp_map, dict):
            stale_mcp = [
                k for k, v in mcp_map.items()
                if isinstance(v, dict)
                and any(tok in str(v.get("tool") or "").lower() for tok in ("terser-index", "terser_index"))
            ]
            for k in stale_mcp:
                mcp_map.pop(k, None)
                changed = True
        if changed:
            _save_store(sid, store, now, env)


def _is_mutating_or_live_mcp(name: str) -> bool:
    # Strip MCP server prefix (`server:tool` or `mcp__server__tool`) so server names
    # like `terser-index` don't misclassify read-only tools such as `search_symbols`.
    tool_part = name.rsplit(":", 1)[-1]
    if "__" in tool_part:
        tool_part = tool_part.rsplit("__", 1)[-1]
    tokens = [t for t in re.split(r"[^a-z0-9]+", tool_part.lower()) if t]
    return any(t in _MUTATING_OR_LIVE_MCP_TOKENS for t in tokens)


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
    if not cfg.get("mcp_refetch_guard", True) or cfg.get("shadow"):
        return None
    if not session_id or not _SID_RE.match(session_id):
        return None
    if not display_name or _is_mutating_or_live_mcp(display_name):
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
