#!/usr/bin/env python3
"""Token Optimizer — Google Antigravity PostToolUse + PreInvocation thrash guard.

Prevents the highest-cost token waste pattern (agents spinning in repeated
identical or failing tool loops) without blocking any tool call:

1. `PostToolUse` calls `record_post_tool_use(home, payload)`:
   - Tracks consecutive identical tool calls (`STREAK_THRESHOLD = 3`, then
     every `REPEAT_AFTER = 3` calls).
   - Tracks repeated failing calls (`FAIL_STREAK_THRESHOLD = 3`, then every
     `REPEAT_AFTER = 3` failures) when `payload["error"]` is non-empty.
   - Queues a single concise 1-line nudge in `<to_dir>/thrash-state/<cid>.json`.

2. `PreInvocation` calls `pop_pending_nudge(home, conversation_id)`:
   - Atomically pops and returns any queued nudge so it is injected once as a
     `systemMessage` and never spammed.

Fail-open everywhere (R15). Pure stdlib. Opt-out via
`TOKEN_OPTIMIZER_THRASH_GUARD=0`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

STREAK_THRESHOLD = 3
FAIL_STREAK_THRESHOLD = 3
REPEAT_AFTER = 3
STALE_SECONDS = 1800.0
_TTL_SECONDS = 7 * 86400
_LABEL_MAX_CHARS = 80
_CONVERSATION_ID_RE = re.compile(r"^[0-9a-fA-F_-]{1,64}$")


def _to_dir(home: Path) -> Path:
    return home / "token-optimizer"


def _state_dir(home: Path) -> Path:
    return _to_dir(home) / "thrash-state"


def _state_path(home: Path, conversation_id: str) -> Path:
    return _state_dir(home) / f"{conversation_id}.json"


def _clean_label(text: str) -> str:
    cleaned = "".join(ch if ch == " " or ch.isprintable() else " " for ch in str(text or ""))
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > _LABEL_MAX_CHARS:
        return cleaned[: _LABEL_MAX_CHARS - 3] + "..."
    return cleaned


def _sweep_stale(sdir: Path, now: float) -> None:
    try:
        if not sdir.is_dir():
            return
        cutoff = now - _TTL_SECONDS
        for p in sdir.glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _load_state(home: Path, conversation_id: str) -> dict:
    path = _state_path(home, conversation_id)
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_state(home: Path, conversation_id: str, state: dict, now: float) -> None:
    sdir = _state_dir(home)
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        _sweep_stale(sdir, now)
        path = _state_path(home, conversation_id)
        fd, tmp = tempfile.mkstemp(prefix=".thrash.", suffix=".tmp", dir=str(sdir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, str(path))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        pass


def _tool_signature(tool_name: str, args: dict) -> tuple[str, str] | None:
    """Return `(hash_key, human_label)` for trackable tool calls."""
    if tool_name == "run_command":
        cmd = args.get("CommandLine")
        if not isinstance(cmd, str) or not cmd.strip():
            return None
        norm = " ".join(cmd.split())
        h = hashlib.sha256(f"run_command:{norm}".encode("utf-8")).hexdigest()[:20]
        return h, _clean_label(norm)
    if tool_name in ("read_file", "view_file", "edit_file", "write_file"):
        raw_path = args.get("Path") or args.get("AbsolutePath") or args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        norm_path = raw_path.lower() if os.name == "nt" else raw_path
        extra = ""
        if tool_name in ("read_file", "view_file"):
            extra = f":{args.get('StartLine')}:{args.get('EndLine')}"
        h = hashlib.sha256(f"{tool_name}:{norm_path}{extra}".encode("utf-8")).hexdigest()[:20]
        return h, _clean_label(f"{tool_name}({Path(raw_path).name})")
    return None


def _should_fire(count: int, threshold: int) -> bool:
    return count >= threshold and ((count - threshold) % REPEAT_AFTER == 0)


def record_post_tool_use(home: Path, payload: dict, *, now: float | None = None) -> None:
    """Update per-conversation streak counters and queue a nudge if thrashing."""
    if os.environ.get("TOKEN_OPTIMIZER_THRASH_GUARD", "").strip() == "0":
        return

    now = time.time() if now is None else float(now)
    conversation_id = str(payload.get("conversationId") or "")
    if not _CONVERSATION_ID_RE.match(conversation_id):
        return

    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return
    tool_name = str(tool_call.get("name") or "")
    args = tool_call.get("args")
    if not isinstance(args, dict):
        return

    sig = _tool_signature(tool_name, args)
    if sig is None:
        return
    key, label = sig
    error_text = str(payload.get("error") or "").strip()

    state = _load_state(home, conversation_id)
    last_ts = float(state.get("last_ts") or 0.0)
    if last_ts and (now - last_ts) > STALE_SECONDS:
        state = {}

    # Track consecutive identical calls.
    if state.get("last_key") == key:
        same_streak = int(state.get("same_streak") or 0) + 1
    else:
        same_streak = 1
    state["last_key"] = key
    state["same_streak"] = same_streak
    state["last_ts"] = now

    # Track per-key failure streaks.
    fail_streaks = state.get("fail_streaks")
    if not isinstance(fail_streaks, dict):
        fail_streaks = {}
    if error_text:
        fail_count = int(fail_streaks.get(key) or 0) + 1
        fail_streaks[key] = fail_count
    else:
        fail_streaks.pop(key, None)
        fail_count = 0
    # Bound fail_streaks map size.
    if len(fail_streaks) > 50:
        fail_streaks = dict(list(fail_streaks.items())[-50:])
    state["fail_streaks"] = fail_streaks

    # Priority: repeated failure > identical repeat.
    nudge: str | None = None
    if _should_fire(fail_count, FAIL_STREAK_THRESHOLD):
        nudge = (
            f"[Token Optimizer] `{label}` has failed {fail_count} times in a row. "
            "Pause and diagnose the root cause before retrying the same step."
        )
    elif _should_fire(same_streak, STREAK_THRESHOLD):
        nudge = (
            f"[Token Optimizer] `{label}` has run {same_streak} times consecutively. "
            "Verify what changed before repeating it again."
        )

    if nudge:
        state["pending_nudge"] = nudge

    _save_state(home, conversation_id, state, now)


def pop_pending_nudge(home: Path, conversation_id: str, *, now: float | None = None) -> str | None:
    """Return and clear any queued thrash nudge for `conversation_id`."""
    if os.environ.get("TOKEN_OPTIMIZER_THRASH_GUARD", "").strip() == "0":
        return None
    if not _CONVERSATION_ID_RE.match(str(conversation_id or "")):
        return None
    state = _load_state(home, conversation_id)
    nudge = state.get("pending_nudge")
    if not isinstance(nudge, str) or not nudge.strip():
        return None
    state.pop("pending_nudge", None)
    _save_state(home, conversation_id, state, time.time() if now is None else float(now))
    return nudge
