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
_MD_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*$")


def build_code_outline(file_path: str, text: str) -> str | None:
    """Build a compact line-numbered symbol or doc-heading outline for a large file.

    Prefers Terser's bundled ``structure_map`` when available, falling back to
    Python's ``ast`` module (for .py), Markdown heading TOC (for .md), or a fast regex extractor.
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

    # 3. Generic multi-language regex outline
    rows = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _GENERIC_SYM_RE.match(line):
            rows.append(f"L{lineno}: {line.strip()[:100]}")
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

    try:
        raw_bytes = target.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw_bytes[:4096]:
        return None

    now = time.time() if now is None else float(now)
    sha = hashlib.sha256(raw_bytes).hexdigest()
    key = _norm_path(file_path)
    basename = target.name
    store = _load_store(session_id, env)
    reads = store["reads"]
    prev = reads.get(key)

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
            prev["start"] = start_line
            prev["end"] = end_line
            prev["soft_denies"] = 0
            prev["ts"] = now
            if text_snapshot is not None:
                prev["snapshot"] = text_snapshot
            if start_line is None and end_line is None:
                meta = store.setdefault("meta", {})
                meta["outline_overrides"] = int(meta.get("outline_overrides") or 0) + 1
            _save_store(session_id, store, now, env)
            return None

        # Tier 0: Unchanged re-read
        if prev_sha == sha and _range_covered(prev_start, prev_end, start_line, end_line):
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

        # Tier 1: Compact unified delta diff on small edits
        if (
            cfg.get("delta_diff", True)
            and prev_sha != sha
            and soft_denies < _SOFT_DENY_CAP
            and not cfg.get("shadow")
            and isinstance(prev.get("snapshot"), str)
            and text_snapshot is not None
            and _range_covered(prev_start, prev_end, start_line, end_line)
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

    # Tier 2: First unranged read of a LARGE code/doc file -> inline line-numbered outline ONCE
    # (Auto-backs off if the session already overrode `outline_backoff_limit` outlines for full reads.)
    backoff_limit = int(cfg.get("outline_backoff_limit", 2))
    outline_overrides = int((store.get("meta") or {}).get("outline_overrides") or 0)
    if (
        prev is None
        and start_line is None
        and end_line is None
        and cfg.get("outline_first", True)
        and (backoff_limit <= 0 or outline_overrides < backoff_limit)
        and not cfg.get("shadow")
        and st.st_size >= int(cfg.get("outline_min_bytes", 10240))
    ):
        text_for_outline = text_snapshot or raw_bytes.decode("utf-8", errors="replace")
        total_lines = text_for_outline.count("\n") + 1
        if total_lines >= int(cfg.get("outline_min_lines", 250)):
            outline = build_code_outline(file_path, text_for_outline)
            if outline:
                reads[key] = {
                    "sha": sha,
                    "start": None,
                    "end": None,
                    "soft_denies": 1,
                    "outline_only": True,
                    "ts": now,
                }
                _save_store(session_id, store, now, env)
                range_hint = "StartLine/EndLine" if host == "antigravity" else "offset/limit"
                reason = (
                    f"[Terser] `{basename}` is {total_lines} lines ({st.st_size:,} B). "
                    f"Outline (exact line spans):\n{outline}\n\n"
                    f"Read only the needed span with {range_hint} (or `terser-index` `get_symbol_source`), "
                    "or retry this read immediately to load the full file."
                )
                record_savings("read_outline", len(raw_bytes), len(reason), host=host, detail=basename, env=env)
                return {"action": "deny", "reason": reason}

    # Tier 3: First read / wider range / large change -> record snapshot and allow
    merged_start, merged_end = start_line, end_line
    if isinstance(prev, dict) and prev.get("sha") == sha:
        p_s, p_e = prev.get("start"), prev.get("end")
        merged_start = None if (p_s is None or start_line is None) else min(p_s, start_line)
        merged_end = None if (p_e is None or end_line is None) else max(p_e, end_line)

    entry: dict[str, Any] = {
        "sha": sha,
        "start": merged_start,
        "end": merged_end,
        "soft_denies": 0,
        "outline_only": False,
        "ts": now,
    }
    if text_snapshot is not None:
        entry["snapshot"] = text_snapshot
    reads[key] = entry
    _save_store(session_id, store, now, env)
    return None


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
        entry = store["reads"].get(key)
        if isinstance(entry, dict):
            entry["soft_denies"] = 0
            entry["outline_only"] = False
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
            record_savings("mcp_dedup", 2000, len(reason), host=host, detail=display_name, env=env)
            return {"action": "deny", "reason": reason}

    mcp[fp] = {"tool": display_name, "ts": now, "soft_denies": 0}
    _save_store(session_id, store, now, env)
    return None
