"""Terser — Local SQLite FTS5 Context Sandbox.

Instead of forcing an agent to dump an entire 100 KB archived output back into
context via `terse expand <id>`, every oversized command or MCP output is chunked
and indexed into a local SQLite FTS5 database (`~/.terser/sandbox.db`).

Agents can then run:
  - `terse query <id> "<keywords>"` -> returns only the top matching 30-line chunks (~300 tok)
  - `terse expand <id> [start_line end_line]` -> returns an exact line range or full text

100% Python stdlib (`sqlite3`), local-only, auto-pruned after 7 days, fail-open.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from runtime.config import state_home

_TTL_SECONDS = 7 * 86400
_MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MB total archive cap
_CHUNK_LINES = 30
_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")


def _db_path(env: dict[str, str] | None = None) -> Path:
    return state_home(env) / "sandbox.db"


def _connect(env: dict[str, str] | None = None) -> sqlite3.Connection:
    p = _db_path(env)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=3.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS blobs (
            id TEXT PRIMARY KEY,
            command TEXT,
            created_at REAL,
            total_lines INTEGER,
            total_bytes INTEGER,
            content TEXT
        )
        """
    )
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                blob_id UNINDEXED,
                start_line UNINDEXED,
                end_line UNINDEXED,
                body
            )
            """
        )
    except sqlite3.OperationalError:
        # Fallback if a minimal Python build lacks FTS5: plain chunks table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks_plain (
                blob_id TEXT,
                start_line INTEGER,
                end_line INTEGER,
                body TEXT
            )
            """
        )
    return conn


def _has_fts5(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    return row is not None


def _delete_ids(conn: sqlite3.Connection, ids: list[str]) -> None:
    if not ids:
        return
    use_fts = _has_fts5(conn)
    for oid in ids:
        conn.execute("DELETE FROM blobs WHERE id = ?", (oid,))
        if use_fts:
            conn.execute("DELETE FROM chunks_fts WHERE blob_id = ?", (oid,))
        else:
            conn.execute("DELETE FROM chunks_plain WHERE blob_id = ?", (oid,))


def _prune_old(conn: sqlite3.Connection, now: float, max_bytes: int = _MAX_TOTAL_BYTES) -> None:
    cutoff = now - _TTL_SECONDS
    old_ids = [r[0] for r in conn.execute("SELECT id FROM blobs WHERE created_at < ?", (cutoff,)).fetchall()]
    _delete_ids(conn, old_ids)

    # Size-cap pruning: evict oldest blobs while total stored bytes exceed max_bytes
    row = conn.execute("SELECT COALESCE(SUM(total_bytes), 0) FROM blobs").fetchone()
    total = int(row[0] or 0) if row else 0
    if total > max_bytes:
        evict: list[str] = []
        for bid, bsz in conn.execute("SELECT id, total_bytes FROM blobs ORDER BY created_at ASC").fetchall():
            if total <= max_bytes:
                break
            evict.append(str(bid))
            total -= int(bsz or 0)
        _delete_ids(conn, evict)


def get_latest_archive_id(env: dict[str, str] | None = None) -> str | None:
    """Return the most recently indexed archive ID in sandbox.db, if any."""
    try:
        if not _db_path(env).is_file():
            return None
        with closing(_connect(env)) as conn:
            row = conn.execute("SELECT id FROM blobs ORDER BY created_at DESC LIMIT 1").fetchone()
            return str(row[0]) if row else None
    except Exception:
        return None


def _resolve_id(archive_id: str, env: dict[str, str] | None = None) -> str | None:
    raw = str(archive_id or "").strip()
    if raw in ("--last", "last", "latest"):
        return get_latest_archive_id(env)
    return raw if _ID_RE.match(raw) else None


def store_output(
    raw_text: str,
    *,
    command: str = "",
    env: dict[str, str] | None = None,
) -> str | None:
    """Index `raw_text` into sandbox.db and return its short archive ID."""
    if not raw_text:
        return None
    now = time.time()
    aid = hashlib.sha256(f"{now}:{command}:{len(raw_text)}".encode("utf-8")).hexdigest()[:10]
    lines = raw_text.splitlines()
    try:
        with closing(_connect(env)) as conn:
            with conn:
                _prune_old(conn, now)
                conn.execute(
                    "INSERT OR REPLACE INTO blobs (id, command, created_at, total_lines, total_bytes, content) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (aid, command[:200], now, len(lines), len(raw_text.encode("utf-8", errors="replace")), raw_text),
                )
                use_fts = _has_fts5(conn)
                table = "chunks_fts" if use_fts else "chunks_plain"
                rows = []
                for start in range(0, max(1, len(lines)), _CHUNK_LINES):
                    chunk = "\n".join(lines[start : start + _CHUNK_LINES])
                    if chunk.strip():
                        rows.append((aid, start + 1, min(len(lines), start + _CHUNK_LINES), chunk))
                if rows:
                    conn.executemany(
                        f"INSERT INTO {table} (blob_id, start_line, end_line, body) VALUES (?, ?, ?, ?)",
                        rows,
                    )
        return aid
    except Exception:
        return None


def query_output(
    archive_id: str,
    query: str,
    *,
    limit: int = 5,
    env: dict[str, str] | None = None,
) -> str | None:
    """Search an archived blob with FTS5 (or case-insensitive substring fallback)."""
    resolved = _resolve_id(archive_id, env)
    if not resolved or not (query or "").strip():
        return None
    archive_id = resolved
    try:
        with closing(_connect(env)) as conn:
            meta = conn.execute(
                "SELECT command, total_lines, total_bytes FROM blobs WHERE id = ?",
                (archive_id,),
            ).fetchone()
            if meta is None:
                return None
            cmd, total_lines, total_bytes = meta
            terms = [t for t in re.findall(r"[A-Za-z0-9_./:-]+", query) if len(t) >= 2]
            hits: list[tuple[int, int, str]] = []
            if _has_fts5(conn) and terms:
                # Safe FTS5 OR query with quoted tokens so punctuation never causes syntax errors
                fts_q = " OR ".join('"' + t.replace('"', "") + '"' for t in terms[:8])
                try:
                    hits = conn.execute(
                        "SELECT start_line, end_line, body FROM chunks_fts "
                        "WHERE blob_id = ? AND chunks_fts MATCH ? ORDER BY rank LIMIT ?",
                        (archive_id, fts_q, limit),
                    ).fetchall()
                except sqlite3.OperationalError:
                    hits = []
            if not hits:
                # Substring fallback
                table = "chunks_fts" if _has_fts5(conn) else "chunks_plain"
                pat = f"%{query.strip()}%"
                hits = conn.execute(
                    f"SELECT start_line, end_line, body FROM {table} "
                    "WHERE blob_id = ? AND body LIKE ? LIMIT ?",
                    (archive_id, pat, limit),
                ).fetchall()

            if not hits:
                return (
                    f"[Terser Sandbox `{archive_id}` ({total_lines} lines, {total_bytes:,} B, cmd=`{cmd}`)]: "
                    f"no matches for `{query}`."
                )
            parts = [
                f"[Terser Sandbox `{archive_id}`: {len(hits)} matching chunk(s) of {total_lines} lines for `{query}`]"
            ]
            for s_ln, e_ln, body in hits:
                parts.append(f"--- L{s_ln}-L{e_ln} ---\n{body}")
            return "\n".join(parts)
    except Exception:
        return None


def expand_output(
    archive_id: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Return a targeted line range (or full text) from sandbox.db."""
    resolved = _resolve_id(archive_id, env)
    if not resolved:
        return None
    archive_id = resolved
    try:
        with closing(_connect(env)) as conn:
            row = conn.execute("SELECT content FROM blobs WHERE id = ?", (archive_id,)).fetchone()
            if row is None:
                return None
            content = str(row[0])
            if start_line is None and end_line is None:
                return content
            lines = content.splitlines()
            s = max(0, (start_line - 1) if start_line else 0)
            e = min(len(lines), end_line) if end_line else len(lines)
            return "\n".join(f"L{idx}: {ln}" for idx, ln in enumerate(lines[s:e], start=s + 1))
    except Exception:
        return None
