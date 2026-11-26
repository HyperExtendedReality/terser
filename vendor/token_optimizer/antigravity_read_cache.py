#!/usr/bin/env python3
"""Token Optimizer — Google Antigravity bounded read cache & MCP re-fetch guard.

PreToolUse optimization for Antigravity 2.0 that saves input tokens without
degrading output quality:

1. Bounded redundant `read_file` / `view_file` interception:
   - Tracks per-conversation `(path, StartLine, EndLine)` reads + content hash
     and a bounded snapshot (<= 128 KB) under `<to_dir>/read-cache/`.
   - If the agent re-reads the exact same unchanged file & covered line range,
     soft-denies ONCE with a concise reminder to reuse the already-loaded
     content. The very next retry always passes through (e.g. after `/compact`).
   - If the file changed slightly since the last read and a compact unified
     diff (<= 60 lines and < 40% of the file) is available, returns the exact
     diff once instead of re-reading the entire file.
   - Skips files > 1 MB, binary files, and `ByteOffset` continuation reads.

2. Bounded duplicate read-only MCP call guard (`call_mcp_tool` / `mcp_*`):
   - If the identical read-only MCP tool + arguments are called again within
     120 seconds, soft-denies ONCE with a reminder to reuse the prior result.
   - Skips mutating and live-state tool names (`write`, `edit`, `create`,
     `delete`, `update`, `post`, `send`, `run`, `exec`, `status`, `poll`, etc.).

Fail-open by contract (R15): any error returns `{}` so the tool call proceeds
untouched. Pure stdlib.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

_MAX_FILE_BYTES = 1024 * 1024  # 1 MB: never intercept reads of huge files
_MAX_SNAPSHOT_BYTES = 128 * 1024  # 128 KB: cap stored snapshots for delta diff
_MAX_STORE_ENTRIES = 150
_STORE_TTL_SECONDS = 7 * 86400
_SOFT_DENY_CAP = 1  # max soft-denies per key before allowing through
_MCP_DEDUP_WINDOW_SECONDS = 120.0
_DELTA_MAX_DIFF_LINES = 60
_DELTA_MAX_RATIO = 0.40

_CONVERSATION_ID_RE = re.compile(r"^[0-9a-fA-F_-]{1,64}$")

_MUTATING_OR_LIVE_MCP_TOKENS = frozenset({
    "write", "edit", "create", "delete", "remove", "drop", "update", "patch",
    "put", "post", "send", "submit", "publish", "deploy", "run", "exec",
    "execute", "invoke", "trigger", "start", "stop", "kill", "cancel",
    "status", "poll", "wait", "watch", "tail", "logs", "time", "now",
    "clock", "health", "ping", "refresh", "invalidate", "register",
})


def _to_dir(home: Path) -> Path:
    return home / "token-optimizer"


def _cache_dir(home: Path) -> Path:
    return _to_dir(home) / "read-cache"


def _cache_path(home: Path, conversation_id: str) -> Path:
    return _cache_dir(home) / f"{conversation_id}.json"


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


def _load_store(home: Path, conversation_id: str) -> dict:
    path = _cache_path(home, conversation_id)
    try:
        if not path.is_file():
            return {"reads": {}, "mcp": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"reads": {}, "mcp": {}}
        reads = data.get("reads") if isinstance(data.get("reads"), dict) else {}
        mcp = data.get("mcp") if isinstance(data.get("mcp"), dict) else {}
        return {"reads": reads, "mcp": mcp}
    except (OSError, json.JSONDecodeError, ValueError):
        return {"reads": {}, "mcp": {}}


def _save_store(home: Path, conversation_id: str, store: dict, now: float) -> None:
    cdir = _cache_dir(home)
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        _sweep_stale(cdir, now)
        # Bound entries to prevent unbounded disk growth.
        for bucket_key in ("reads", "mcp"):
            bucket = store.get(bucket_key)
            if isinstance(bucket, dict) and len(bucket) > _MAX_STORE_ENTRIES:
                ordered = sorted(
                    bucket.items(),
                    key=lambda kv: kv[1].get("ts", 0) if isinstance(kv[1], dict) else 0,
                    reverse=True,
                )[:_MAX_STORE_ENTRIES]
                store[bucket_key] = dict(ordered)

        path = _cache_path(home, conversation_id)
        fd, tmp = tempfile.mkstemp(prefix=".rc.", suffix=".tmp", dir=str(cdir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(store, fh)
            os.replace(tmp, str(path))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        pass


def _normalize_path(raw: str) -> str:
    try:
        p = str(Path(raw).resolve(strict=False))
    except (OSError, ValueError):
        p = str(raw)
    return p.lower() if os.name == "nt" else p


def _parse_int(val) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _range_covered(prev_start: int | None, prev_end: int | None,
                   req_start: int | None, req_end: int | None) -> bool:
    """True when the previously read line range covers the requested range."""
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
    """Return a compact unified diff if the change is small, else None."""
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
    if not diff_lines:
        return None
    if len(diff_lines) > _DELTA_MAX_DIFF_LINES:
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


def check_read_file(home: Path, payload: dict, *, now: float | None = None) -> dict:
    """Bounded PreToolUse read-cache + delta-diff check for read_file/view_file."""
    if os.environ.get("TOKEN_OPTIMIZER_READ_CACHE", "").strip() == "0":
        return {}

    now = time.time() if now is None else float(now)
    conversation_id = str(payload.get("conversationId") or "")
    if not _CONVERSATION_ID_RE.match(conversation_id):
        return {}

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return {}
    args = tool_call.get("args")
    if not isinstance(args, dict):
        return {}

    # Never interfere with byte-offset chunked continuation reads.
    if args.get("ByteOffset") not in (None, 0):
        return {}

    raw_path = args.get("Path") or args.get("AbsolutePath") or args.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return {}

    target = Path(raw_path)
    try:
        st = target.stat()
    except OSError:
        return {}
    if not target.is_file() or st.st_size > _MAX_FILE_BYTES:
        return {}

    try:
        raw_bytes = target.read_bytes()
    except OSError:
        return {}

    # Skip binary files (NUL byte heuristic).
    if b"\x00" in raw_bytes[:4096]:
        return {}

    sha = hashlib.sha256(raw_bytes).hexdigest()
    start_line = _parse_int(args.get("StartLine"))
    end_line = _parse_int(args.get("EndLine"))
    key = _normalize_path(raw_path)
    basename = target.name

    store = _load_store(home, conversation_id)
    reads = store["reads"]
    prev = reads.get(key)

    text_snapshot: str | None = None
    if len(raw_bytes) <= _MAX_SNAPSHOT_BYTES:
        text_snapshot = raw_bytes.decode("utf-8", errors="replace")

    if isinstance(prev, dict):
        prev_sha = prev.get("sha")
        prev_start = prev.get("start")
        prev_end = prev.get("end")
        soft_denies = int(prev.get("soft_denies") or 0)

        if prev_sha == sha and _range_covered(prev_start, prev_end, start_line, end_line):
            if soft_denies < _SOFT_DENY_CAP:
                prev["soft_denies"] = soft_denies + 1
                prev["ts"] = now
                _save_store(home, conversation_id, store, now)
                rng = _format_range(start_line, end_line)
                return {
                    "decision": "deny",
                    "reason": (
                        f"[Token Optimizer] `{basename}` ({rng}) is unchanged since your "
                        "earlier read in this session. Reuse the content already in context, "
                        "or retry this read if you need it refreshed (e.g. after /compact)."
                    ),
                }
            # Cap reached: reset soft_denies and allow the refresh through.
            prev["soft_denies"] = 0
            prev["ts"] = now
            _save_store(home, conversation_id, store, now)
            return {}

        if (
            prev_sha != sha
            and soft_denies < _SOFT_DENY_CAP
            and isinstance(prev.get("snapshot"), str)
            and text_snapshot is not None
            and _range_covered(prev_start, prev_end, start_line, end_line)
        ):
            diff = _compact_unified_diff(
                prev["snapshot"], text_snapshot, basename, start_line, end_line
            )
            if diff:
                prev["sha"] = sha
                prev["snapshot"] = text_snapshot
                prev["soft_denies"] = soft_denies + 1
                prev["ts"] = now
                _save_store(home, conversation_id, store, now)
                return {
                    "decision": "deny",
                    "reason": (
                        f"[Token Optimizer] `{basename}` changed slightly since your last "
                        f"read. Exact unified diff:\n```diff\n{diff}\n```\n"
                        "(Retry read_file if you need the full file.)"
                    ),
                }

    # First read, wider range, or large modification: record and allow.
    merged_start = start_line
    merged_end = end_line
    if isinstance(prev, dict) and prev.get("sha") == sha:
        p_s = prev.get("start")
        p_e = prev.get("end")
        if p_s is None or start_line is None:
            merged_start = None
        else:
            merged_start = min(p_s, start_line)
        if p_e is None or end_line is None:
            merged_end = None
        else:
            merged_end = max(p_e, end_line)

    entry = {
        "sha": sha,
        "start": merged_start,
        "end": merged_end,
        "soft_denies": 0,
        "ts": now,
    }
    if text_snapshot is not None:
        entry["snapshot"] = text_snapshot
    reads[key] = entry
    _save_store(home, conversation_id, store, now)
    return {}


def _is_mutating_or_live_mcp(name: str) -> bool:
    tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]
    return any(t in _MUTATING_OR_LIVE_MCP_TOKENS for t in tokens)


def check_mcp_refetch(home: Path, payload: dict, *, now: float | None = None) -> dict:
    """Bounded PreToolUse duplicate-call guard for read-only MCP tools."""
    if os.environ.get("TOKEN_OPTIMIZER_MCP_REFETCH_GUARD", "").strip() == "0":
        return {}

    now = time.time() if now is None else float(now)
    conversation_id = str(payload.get("conversationId") or "")
    if not _CONVERSATION_ID_RE.match(conversation_id):
        return {}

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return {}
    tool_name = str(tool_call.get("name") or "")
    args = tool_call.get("args")
    if not isinstance(args, dict):
        return {}

    if tool_name == "call_mcp_tool":
        server = str(args.get("ServerName") or "")
        inner_tool = str(args.get("ToolName") or "")
        display_name = f"{server}:{inner_tool}" if server else inner_tool
        inner_args = args.get("Arguments")
    else:
        display_name = tool_name
        inner_args = args

    if not display_name or _is_mutating_or_live_mcp(display_name):
        return {}

    try:
        canonical = json.dumps(
            {"tool": display_name, "args": inner_args},
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return {}
    fp = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    store = _load_store(home, conversation_id)
    mcp = store["mcp"]
    prev = mcp.get(fp)

    if isinstance(prev, dict):
        elapsed = now - float(prev.get("ts") or 0.0)
        soft_denies = int(prev.get("soft_denies") or 0)
        if 0.0 <= elapsed <= _MCP_DEDUP_WINDOW_SECONDS and soft_denies < _SOFT_DENY_CAP:
            prev["soft_denies"] = soft_denies + 1
            _save_store(home, conversation_id, store, now)
            secs = max(1, int(elapsed))
            return {
                "decision": "deny",
                "reason": (
                    f"[Token Optimizer] Identical MCP call `{display_name}` with the same "
                    f"arguments ran {secs}s ago in this session. Reuse the earlier result, "
                    "or retry if you expect live state to have changed."
                ),
            }

    mcp[fp] = {"tool": display_name, "ts": now, "soft_denies": 0}
    _save_store(home, conversation_id, store, now)
    return {}
