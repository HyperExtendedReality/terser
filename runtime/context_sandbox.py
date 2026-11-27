"""Terser — Local SQLite FTS5 Context Sandbox & Session Continuity Store.

Every oversized command, MCP, or indexed doc/log output is:
  1. Scrubbed for credentials/API keys before hitting disk.
  2. Chunked with Markdown-heading & code-fence awareness.
  3. Indexed into a local SQLite FTS5 database (`~/.terser/sandbox.db`) with
     Porter stemming (`porter unicode61`) and BM25 relevance ranking.

Also maintains a lightweight structured `session_events` log (recent edits &
errors per workspace) for instant, zero-latency compaction/session continuity.

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

from runtime.config import ensure_vendor_paths, normalize_workspace, state_home

_TTL_SECONDS = 7 * 86400
_MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MB total archive cap
_CHUNK_LINES = 30
_CHUNK_OVERLAP = 2
_MAX_BLOCK_LINES = 80
_MAX_SESSION_EVENTS = 200
_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S")
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")

# Lightweight fallback secret patterns if vendored credential_patterns is unavailable
_FALLBACK_SECRET_RE = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    r")"
)


def redact_text(text: str, *, is_command: bool = False) -> str:
    """Scrub API keys, tokens, and secrets before persisting text to disk."""
    if not text:
        return ""
    try:
        ensure_vendor_paths()
        import credential_patterns  # type: ignore

        return str(credential_patterns.redact_credentials(text, command=is_command))
    except Exception:
        return _FALLBACK_SECRET_RE.sub("[REDACTED]", text)


def _chunk_lines(lines: list[str]) -> list[tuple[int, int, str]]:
    """Structure-aware chunking: respects Markdown headings and keeps code fences intact."""
    if not lines:
        return []
    chunks: list[tuple[int, int, str]] = []
    cur: list[str] = []
    cur_start = 1
    in_fence = False
    fence_marker = ""

    def flush(end_line: int) -> None:
        nonlocal cur
        if cur:
            body = "\n".join(cur)
            if body.strip():
                chunks.append((cur_start, end_line, body))
            cur = []

    for idx, line in enumerate(lines, start=1):
        m_fence = _FENCE_RE.match(line)
        if m_fence:
            marker = m_fence.group(1)
            if not in_fence:
                # Start of fenced code block: flush prior chunk if already substantial
                if len(cur) >= _CHUNK_LINES // 2:
                    flush(idx - 1)
                    cur_start = idx
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            cur.append(line)
            if not in_fence and len(cur) >= _CHUNK_LINES:
                flush(idx)
                cur_start = idx + 1
            continue

        if not in_fence and _HEADING_RE.match(line) and cur:
            flush(idx - 1)
            cur_start = idx
            cur.append(line)
            continue

        if not cur:
            cur_start = idx
        cur.append(line)

        limit = _MAX_BLOCK_LINES if in_fence else _CHUNK_LINES
        if len(cur) >= limit:
            overlap = (
                cur[-_CHUNK_OVERLAP:]
                if (not in_fence and _CHUNK_OVERLAP > 0 and len(cur) > _CHUNK_OVERLAP and idx < len(lines))
                else []
            )
            flush(idx)
            if overlap:
                cur = list(overlap)
                cur_start = idx - len(overlap) + 1
            else:
                cur_start = idx + 1

    flush(len(lines))
    return chunks


def _db_path(env: dict[str, str] | None = None) -> Path:
    return state_home(env) / "sandbox.db"


def _ensure_fts_table(conn: sqlite3.Connection) -> bool:
    """Ensure `chunks_fts` uses Porter stemming (`porter unicode61`), migrating if needed."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    if row is not None:
        sql = str(row[0] or "").lower()
        if "porter" in sql:
            return True
        # Migrate legacy unstemmed FTS5 table in place
        try:
            conn.execute("DROP TABLE chunks_fts")
        except sqlite3.OperationalError:
            return True

    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                blob_id UNINDEXED,
                start_line UNINDEXED,
                end_line UNINDEXED,
                body,
                tokenize='porter unicode61'
            )
            """
        )
    except sqlite3.OperationalError:
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
        return False

    # If we migrated an existing database with blobs, re-populate chunks_fts
    if row is not None:
        for bid, content in conn.execute("SELECT id, content FROM blobs").fetchall():
            chunks = _chunk_lines(str(content or "").splitlines())
            if chunks:
                conn.executemany(
                    "INSERT INTO chunks_fts (blob_id, start_line, end_line, body) VALUES (?, ?, ?, ?)",
                    [(str(bid), s, e, b) for s, e, b in chunks],
                )
    return True


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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS session_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            workspace TEXT,
            kind TEXT,
            summary TEXT,
            created_at REAL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_session_events_ws_ts ON session_events (workspace, created_at DESC)"
    )
    _ensure_fts_table(conn)
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

    # Also prune old session events
    conn.execute("DELETE FROM session_events WHERE created_at < ?", (cutoff,))


def _raw_archives_dir(env: dict[str, str] | None = None) -> Path:
    return state_home(env) / "raw-archives"


def _ingest_raw_archive(conn: sqlite3.Connection, archive_id: str, env: dict[str, str] | None = None) -> bool:
    """Lazy-index a Rust `terser-fast exec` raw sidecar (`~/.terser/raw-archives/<id>.txt`) into sandbox.db."""
    if not _ID_RE.match(archive_id):
        return False
    p = _raw_archives_dir(env) / f"{archive_id}.txt"
    if not p.is_file():
        return False
    try:
        raw_text = p.read_text(encoding="utf-8", errors="replace")
        mtime = p.stat().st_mtime
    except OSError:
        return False
    if not raw_text:
        return False
    safe_text = redact_text(raw_text, is_command=False)
    lines = safe_text.splitlines()
    chunks = _chunk_lines(lines)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO blobs (id, command, created_at, total_lines, total_bytes, content) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (archive_id, "terser-fast exec", mtime, len(lines), len(safe_text.encode("utf-8", errors="replace")), safe_text),
        )
        use_fts = _has_fts5(conn)
        table = "chunks_fts" if use_fts else "chunks_plain"
        if chunks:
            conn.executemany(
                f"INSERT INTO {table} (blob_id, start_line, end_line, body) VALUES (?, ?, ?, ?)",
                [(archive_id, s, e, b) for s, e, b in chunks],
            )
    return True


def get_latest_archive_id(env: dict[str, str] | None = None) -> str | None:
    """Return the most recently indexed archive ID across sandbox.db and Rust raw-archives."""
    best_id: str | None = None
    best_ts: float = -1.0
    try:
        if _db_path(env).is_file():
            with closing(_connect(env)) as conn:
                row = conn.execute("SELECT id, created_at FROM blobs ORDER BY created_at DESC LIMIT 1").fetchone()
                if row:
                    best_id = str(row[0])
                    best_ts = float(row[1] or 0.0)
    except Exception:
        pass
    try:
        rdir = _raw_archives_dir(env)
        if rdir.is_dir():
            for p in rdir.glob("*.txt"):
                if not _ID_RE.match(p.stem):
                    continue
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if mt > best_ts:
                    best_ts = mt
                    best_id = p.stem
    except OSError:
        pass
    return best_id


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
    """Redact credentials, chunk with structure awareness, and index into sandbox.db."""
    if not raw_text:
        return None
    safe_text = redact_text(raw_text, is_command=False)
    safe_cmd = redact_text(command[:200], is_command=True) if command else ""
    now = time.time()
    # Content-addressed ID: identical archived outputs reuse the existing row & FTS5 chunks
    aid = hashlib.sha256(safe_text.encode("utf-8", errors="replace")).hexdigest()[:12]
    lines = safe_text.splitlines()
    try:
        with closing(_connect(env)) as conn:
            with conn:
                _prune_old(conn, now)
                existing = conn.execute("SELECT 1 FROM blobs WHERE id = ?", (aid,)).fetchone()
                if existing is not None:
                    conn.execute(
                        "UPDATE blobs SET created_at = ?, command = ? WHERE id = ?",
                        (now, safe_cmd, aid),
                    )
                    return aid
                chunks = _chunk_lines(lines)
                conn.execute(
                    "INSERT OR REPLACE INTO blobs (id, command, created_at, total_lines, total_bytes, content) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (aid, safe_cmd, now, len(lines), len(safe_text.encode("utf-8", errors="replace")), safe_text),
                )
                use_fts = _has_fts5(conn)
                table = "chunks_fts" if use_fts else "chunks_plain"
                if chunks:
                    conn.executemany(
                        f"INSERT INTO {table} (blob_id, start_line, end_line, body) VALUES (?, ?, ?, ?)",
                        [(aid, s, e, b) for s, e, b in chunks],
                    )
        return aid
    except Exception:
        return None


def index_file(
    file_path: str,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Index a local document/log file into sandbox.db for BM25 FTS5 retrieval."""
    p = Path(file_path).expanduser()
    if not p.is_file():
        return {"error": f"File not found: {file_path}"}
    try:
        raw_bytes = p.read_bytes()
    except OSError as exc:
        return {"error": f"Unable to read {file_path}: {exc}"}
    truncated = len(raw_bytes) > max_bytes
    text = raw_bytes[:max_bytes].decode("utf-8", errors="replace")
    aid = store_output(text, command=f"file:{p.name}", env=env)
    if not aid:
        return {"error": f"Failed to index {file_path} into sandbox.db"}
    lines = text.splitlines()
    return {
        "archive_id": aid,
        "path": str(p.resolve()),
        "lines": len(lines),
        "bytes": len(raw_bytes),
        "truncated": truncated,
        "chunks": len(_chunk_lines(lines)),
        "hint": f'Query via `search_sandbox` or `terse query {aid} "<terms>"`',
    }


def query_output(
    archive_id: str,
    query: str,
    *,
    limit: int = 5,
    env: dict[str, str] | None = None,
) -> str | None:
    """Search an archived blob with FTS5 Porter+BM25 (or substring fallback)."""
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
            if meta is None and _ingest_raw_archive(conn, archive_id, env):
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
                        "WHERE blob_id = ? AND chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
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
            if row is None and _ingest_raw_archive(conn, archive_id, env):
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


def record_session_event(
    session_id: str,
    workspace: str,
    kind: str,
    summary: str,
    *,
    env: dict[str, str] | None = None,
) -> None:
    """Persist a lightweight structured event (edit, error, cmd) for compaction continuity."""
    norm_ws = normalize_workspace(workspace)
    clean_sum = " ".join(redact_text(str(summary or ""), is_command=(kind == "cmd")).split())[:180]
    if not norm_ws or not clean_sum:
        return
    now = time.time()
    try:
        with closing(_connect(env)) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO session_events (session_id, workspace, kind, summary, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (str(session_id or "")[:80], norm_ws, str(kind or "event")[:16], clean_sum, now),
                )
                # Keep only the most recent _MAX_SESSION_EVENTS per workspace
                conn.execute(
                    "DELETE FROM session_events WHERE workspace = ? AND id NOT IN "
                    "(SELECT id FROM session_events WHERE workspace = ? ORDER BY created_at DESC LIMIT ?)",
                    (norm_ws, norm_ws, _MAX_SESSION_EVENTS),
                )
    except Exception:
        pass


def build_continuity_snapshot(
    workspace: str,
    *,
    exclude_session_id: str = "",
    max_chars: int = 500,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Return a compact, priority-tiered list of recent workspace edits & errors."""
    norm_ws = normalize_workspace(workspace)
    if not norm_ws or not _db_path(env).is_file():
        return []
    try:
        with closing(_connect(env)) as conn:
            cutoff = time.time() - 48 * 3600
            if exclude_session_id:
                rows = conn.execute(
                    "SELECT kind, summary FROM session_events "
                    "WHERE workspace = ? AND created_at >= ? AND session_id != ? "
                    "ORDER BY created_at DESC LIMIT 40",
                    (norm_ws, cutoff, exclude_session_id[:80]),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT kind, summary FROM session_events "
                    "WHERE workspace = ? AND created_at >= ? "
                    "ORDER BY created_at DESC LIMIT 40",
                    (norm_ws, cutoff),
                ).fetchall()
    except Exception:
        return []

    if not rows:
        return []

    edits: list[str] = []
    errors: list[str] = []
    for kind, summary in rows:
        s = str(summary or "").strip()
        if not s:
            continue
        if kind == "edit" and s not in edits and len(edits) < 6:
            edits.append(s)
        elif kind == "error" and s not in errors and len(errors) < 3:
            errors.append(s)

    out: list[str] = []
    if edits:
        out.append("- Recent edits: " + ", ".join(edits))
    if errors:
        out.append("- Recent errors: " + " | ".join(errors))

    # Enforce strict character budget
    trimmed: list[str] = []
    total = 0
    for ln in out:
        if total + len(ln) > max_chars:
            rem = max_chars - total - 3
            if rem > 20:
                trimmed.append(ln[:rem].rstrip() + "...")
            break
        trimmed.append(ln)
        total += len(ln) + 1
    return trimmed
