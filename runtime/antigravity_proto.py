"""Stdlib-only read-only protobuf wire-format decoder for Antigravity ``gen_metadata``.

Reads live context-window fill and latest generation usage directly from
``~/.gemini/<surface>/conversations/<conversationId>.db`` without third-party
dependencies and without ever writing to Antigravity's database (``mode=ro``).

Fail-closed: any malformed varint, unknown wire type, or sanity-gate failure
returns ``None``.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

DECODER_VERSION = "ag-v1"

_MAX_BYTES = 5 * 1024 * 1024
_MAX_DEPTH = 6
_MAX_FIELDS = 1000
_DESCEND_PATHS = frozenset({(0, 1), (1, 4), (1, 9), (2, 10)})
_SURFACES = ("antigravity", "antigravity-cli", "antigravity-ide")


def _read_varint(data: bytes, pos: int) -> tuple[int | None, int]:
    value = 0
    shift = 0
    start = pos
    n = len(data)
    while pos < n:
        if pos - start >= 10:
            return None, pos
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not (b & 0x80):
            return value, pos
        shift += 7
        if shift >= 70:
            return None, pos
    return None, pos


def _decode_msg(data: bytes, depth: int = 0) -> dict[int, list[tuple[int, Any]]] | None:
    if depth > _MAX_DEPTH or len(data) > _MAX_BYTES:
        return None
    fields: dict[int, list[tuple[int, Any]]] = {}
    pos = 0
    n = len(data)
    count = 0
    while pos < n:
        key, pos2 = _read_varint(data, pos)
        if key is None:
            return None
        pos = pos2
        count += 1
        if count > _MAX_FIELDS:
            return None
        fnum = key >> 3
        wtype = key & 0x07
        if fnum == 0:
            return None
        if wtype == 0:
            val, pos3 = _read_varint(data, pos)
            if val is None:
                return None
            pos = pos3
            fields.setdefault(fnum, []).append((0, val))
        elif wtype == 1:
            if pos + 8 > n:
                return None
            fields.setdefault(fnum, []).append((1, data[pos:pos + 8]))
            pos += 8
        elif wtype == 2:
            length, pos3 = _read_varint(data, pos)
            if length is None or length < 0 or pos3 + length > n:
                return None
            pos = pos3
            payload = data[pos:pos + length]
            pos += length
            if (depth, fnum) in _DESCEND_PATHS and depth < _MAX_DEPTH:
                sub = _decode_msg(payload, depth + 1)
                if sub is None:
                    return None
                fields.setdefault(fnum, []).append((2, sub))
            else:
                fields.setdefault(fnum, []).append((2, payload))
        elif wtype == 5:
            if pos + 4 > n:
                return None
            fields.setdefault(fnum, []).append((5, data[pos:pos + 4]))
            pos += 4
        else:
            return None
    return fields


def _first_int(msg: dict[int, list[tuple[int, Any]]] | None, fnum: int, default: int = 0) -> int:
    if not isinstance(msg, dict):
        return default
    for wtype, val in msg.get(fnum, ()):
        if wtype == 0 and isinstance(val, int):
            return val
    return default


def _first_sub(msg: dict[int, list[tuple[int, Any]]] | None, fnum: int) -> dict[int, list[tuple[int, Any]]] | None:
    if not isinstance(msg, dict):
        return None
    for wtype, val in msg.get(fnum, ()):
        if wtype == 2 and isinstance(val, dict):
            return val
    return None


def _first_str(msg: dict[int, list[tuple[int, Any]]] | None, fnum: int) -> str:
    if not isinstance(msg, dict):
        return ""
    for wtype, val in msg.get(fnum, ()):
        if wtype == 2 and isinstance(val, (bytes, bytearray)):
            try:
                return val.decode("utf-8", errors="replace").strip()
            except Exception:
                return ""
    return ""


def decode_gen_metadata(blob: bytes) -> dict[str, Any] | None:
    """Decode one ``gen_metadata.data`` protobuf blob into usage + context window fields."""
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        return None
    outer = _decode_msg(bytes(blob), 0)
    gen = _first_sub(outer, 1)
    if gen is None:
        return None
    usage = _first_sub(gen, 4)
    chat_start = _first_sub(gen, 9)
    ctx_win = _first_sub(chat_start, 10)
    est_used = _first_int(ctx_win, 1, 0)
    max_ctx = _first_int(ctx_win, 4, 0)
    inp = _first_int(usage, 2, 0)
    out = _first_int(usage, 3, 0)
    cache_read = _first_int(usage, 5, 0)
    thinking = _first_int(usage, 9, 0)
    model = _first_str(gen, 21)
    # Sanity gate against corrupt/absurd varints
    if max_ctx > 20_000_000 or est_used > 20_000_000:
        return None
    fill: float | None = None
    if max_ctx > 0 and est_used > 0:
        fill = min(1.0, est_used / max_ctx)
    return {
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cache_read,
        "thinking_tokens": thinking,
        "estimated_tokens_used": est_used,
        "max_context_tokens": max_ctx,
        "context_fill": fill,
    }


def _surface_from_transcript(transcript_path: str) -> str | None:
    if not transcript_path:
        return None
    parts = Path(transcript_path).parts
    for surf in _SURFACES:
        if surf in parts:
            return surf
    return None


def _conversation_db_path(
    conversation_id: str,
    transcript_path: str = "",
    env: dict[str, str] | None = None,
) -> Path | None:
    if not conversation_id:
        return None
    e = env if env is not None else os.environ
    ag_home = (e.get("TOKEN_OPTIMIZER_ANTIGRAVITY_HOME") or "").strip()
    base = Path(os.path.expanduser(ag_home)) if ag_home else (Path.home() / ".gemini")
    preferred = _surface_from_transcript(transcript_path)
    surfaces = [preferred] + [s for s in _SURFACES if s != preferred] if preferred else list(_SURFACES)
    for surf in surfaces:
        cand = base / surf / "conversations" / f"{conversation_id}.db"
        if cand.is_file():
            return cand
    return None


def live_context_fill(
    conversation_id: str,
    transcript_path: str = "",
    env: dict[str, str] | None = None,
) -> float | None:
    """Return latest context-window fill fraction (0.0..1.0) from Antigravity's conversation DB."""
    db = _conversation_db_path(conversation_id, transcript_path, env)
    if db is None:
        return None
    uri = f"file:{db.as_posix()}?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=0.25)
        cur = conn.execute("SELECT data FROM gen_metadata ORDER BY rowid DESC LIMIT 5")
        for (blob,) in cur.fetchall():
            if not isinstance(blob, (bytes, bytearray)):
                continue
            rec = decode_gen_metadata(bytes(blob))
            if rec and isinstance(rec.get("context_fill"), float):
                return rec["context_fill"]
    except (sqlite3.Error, OSError):
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return None
