"""Terser — Local SQLite FTS5 Context Sandbox & Session Continuity Store.

Every oversized command, MCP, or indexed doc/log output is:
  1. Scrubbed for credentials/API keys before hitting disk.
  2. Chunked with Markdown-heading & code-fence awareness.
  3. Indexed into a local SQLite FTS5 database (`~/.terser/sandbox.db`) with
     Porter stemming (`porter unicode61`) and BM25 relevance ranking, plus a
     trigram table for partial-word matches. Both rankings are merged with
     reciprocal rank fusion, and a query with no hits gets one spelling
     correction drawn from the archive's own words.

Also maintains a lightweight structured `session_events` log (recent edits &
errors per workspace) for instant, zero-latency compaction/session continuity.

100% Python stdlib (`sqlite3`), local-only, auto-pruned after 7 days, fail-open.
"""
from __future__ import annotations

import difflib
import hashlib
import os
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
_RRF_K = 60
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_VOCAB_MAX_CHARS = 2 * 1024 * 1024

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


# Host whose agent will read emitted retrieval hints. Set by the hook dispatcher.
# Agent hosts get MCP `order(...)` calls (always available, no PATH dependency);
# standalone CLI use keeps the `terse` shell form.
_HINT_HOST: str | None = None
_MCP_HINT_HOSTS = frozenset({"claude", "codex", "antigravity", "agent"})


def set_hint_host(host: str | None) -> None:
    global _HINT_HOST
    _HINT_HOST = (host or "").strip().lower() or None


def _hint_host(env: dict[str, str] | None = None) -> str | None:
    if _HINT_HOST:
        return _HINT_HOST
    import os

    src = env if env is not None else os.environ
    # TERSER_CLIENT_ID is set by the MCP server configs for each agent host.
    raw = src.get("TERSER_HINT_HOST") or src.get("TERSER_CLIENT_ID") or ""
    return raw.strip().lower() or None


def retrieval_hint(archive_id: str, mode: str = "both", *, env: dict[str, str] | None = None) -> str:
    """Return a retrieval instruction for an archived output.

    ``mode``: ``query`` (FTS search), ``expand`` (line range), ``both``, or ``raw`` (full text).
    """
    aid = str(archive_id)
    if _hint_host(env) in _MCP_HINT_HOSTS:
        q = f'order(action="search_sandbox", args={{"archive_id": "{aid}", "query": "<terms>"}})'
        e = f'order(action="expand_sandbox", args={{"archive_id": "{aid}", "start_line": 1, "end_line": 100}})'
        r = f'order(action="expand_sandbox", args={{"archive_id": "{aid}"}})'
    else:
        q = f'terse query {aid} "<terms>"'
        e = f"terse expand {aid} 1 100"
        r = f"terse expand {aid} --raw"
    if mode == "query":
        return f"`{q}`"
    if mode == "expand":
        return f"`{e}`"
    if mode == "raw":
        return f"`{r}`"
    return f"`{q}` or `{e}`"


def redact_text(text: str, *, is_command: bool = False) -> str:
    """Scrub API keys, tokens, and secrets before persisting text to disk."""
    if not text:
        return ""
    try:
        from runtime import redaction

        return redaction.redact(text, command=is_command)
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


_INITIALIZED_DBS: dict[str, int] = {}
_HAS_FTS5_CACHE: dict[str, bool] = {}
_HAS_TRIGRAM_CACHE: dict[str, bool] = {}


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
    key = str(p)
    cached_ver = _INITIALIZED_DBS.get(key)
    if cached_ver is not None and p.is_file():
        try:
            c = sqlite3.connect(key, timeout=3.0)
            row = c.execute("PRAGMA schema_version").fetchone()
            if row is not None and int(row[0]) == cached_ver:
                c.execute("PRAGMA synchronous=NORMAL")
                c.execute("PRAGMA temp_store=MEMORY")
                return c
            c.close()
        except sqlite3.DatabaseError:
            pass
        _INITIALIZED_DBS.pop(key, None)
        _HAS_FTS5_CACHE.pop(key, None)
        _HAS_TRIGRAM_CACHE.pop(key, None)

    p.parent.mkdir(parents=True, exist_ok=True)

    def _open_and_init() -> sqlite3.Connection:
        c = sqlite3.connect(key, timeout=3.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA temp_store=MEMORY")
        c.execute(
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
        return c

    try:
        conn = _open_and_init()
    except sqlite3.DatabaseError:
        heal_sqlite_db(p)
        conn = _open_and_init()
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
    has_fts = _ensure_fts_table(conn)
    _ensure_blob_session_column(conn)
    has_tri = _ensure_trigram_table(conn)
    ver_row = conn.execute("PRAGMA schema_version").fetchone()
    if ver_row is not None:
        _INITIALIZED_DBS[key] = int(ver_row[0])
    _HAS_FTS5_CACHE[key] = has_fts
    _HAS_TRIGRAM_CACHE[key] = has_tri
    return conn


def _ensure_blob_session_column(conn: sqlite3.Connection) -> None:
    """Add `blobs.session_id` to older databases so archives can be purged per session."""
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(blobs)").fetchall()}
        if "session_id" not in cols:
            conn.execute("ALTER TABLE blobs ADD COLUMN session_id TEXT DEFAULT ''")
    except sqlite3.Error:
        pass


def _ensure_trigram_table(conn: sqlite3.Connection) -> bool:
    """Ensure `chunks_tri` (FTS5 trigram, SQLite >= 3.34) exists; backfill it once from chunks_fts."""
    if _has_trigram(conn):
        return True
    if not _has_fts5(conn):
        return False
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_tri USING fts5(
                blob_id UNINDEXED,
                start_line UNINDEXED,
                end_line UNINDEXED,
                body,
                tokenize='trigram'
            )
            """
        )
    except sqlite3.OperationalError:
        return False
    try:
        with conn:
            rows = conn.execute(
                "SELECT blob_id, start_line, end_line, body FROM chunks_fts"
            ).fetchall()
            if rows:
                conn.executemany(
                    "INSERT INTO chunks_tri (blob_id, start_line, end_line, body) VALUES (?, ?, ?, ?)",
                    rows,
                )
    except sqlite3.Error:
        pass
    return True


def _has_trigram(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_tri'"
    ).fetchone()
    return row is not None


def _insert_chunks(conn: sqlite3.Connection, archive_id: str, chunks: list[tuple[int, int, str]]) -> None:
    """Write chunks to every search table that exists (Porter FTS5 or plain, plus trigram)."""
    if not chunks:
        return
    rows = [(archive_id, s, e, b) for s, e, b in chunks]
    table = "chunks_fts" if _has_fts5(conn) else "chunks_plain"
    conn.executemany(
        f"INSERT INTO {table} (blob_id, start_line, end_line, body) VALUES (?, ?, ?, ?)", rows
    )
    if _has_trigram(conn):
        conn.executemany(
            "INSERT INTO chunks_tri (blob_id, start_line, end_line, body) VALUES (?, ?, ?, ?)", rows
        )


def _session_from_env(env: dict[str, str] | None = None) -> str:
    src = env if env is not None else os.environ
    for key in ("TERSER_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_THREAD_ID"):
        val = str(src.get(key) or "").strip()
        if val:
            return val[:80]
    return ""


def _has_fts5(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    return row is not None


def _delete_ids(conn: sqlite3.Connection, ids: list[str]) -> None:
    if not ids:
        return
    use_fts = _has_fts5(conn)
    use_tri = _has_trigram(conn)
    for oid in ids:
        conn.execute("DELETE FROM blobs WHERE id = ?", (oid,))
        if use_fts:
            conn.execute("DELETE FROM chunks_fts WHERE blob_id = ?", (oid,))
        else:
            conn.execute("DELETE FROM chunks_plain WHERE blob_id = ?", (oid,))
        if use_tri:
            conn.execute("DELETE FROM chunks_tri WHERE blob_id = ?", (oid,))


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
        _insert_chunks(conn, archive_id, chunks)
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
    require_exact: bool = False,
    session_id: str | None = None,
) -> str | None:
    """Redact credentials, chunk with structure awareness, and index into sandbox.db."""
    if not raw_text:
        return None
    safe_text = redact_text(raw_text, is_command=False)
    if require_exact and safe_text != raw_text:
        return None
    safe_cmd = redact_text(command[:200], is_command=True) if command else ""
    now = time.time()
    sess = (session_id if session_id is not None else _session_from_env(env))[:80]
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
                        "UPDATE blobs SET created_at = ?, command = ?, session_id = ? WHERE id = ?",
                        (now, safe_cmd, sess, aid),
                    )
                    return aid
                chunks = _chunk_lines(lines)
                conn.execute(
                    "INSERT OR REPLACE INTO blobs (id, command, created_at, total_lines, total_bytes, content, session_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (aid, safe_cmd, now, len(lines), len(safe_text.encode("utf-8", errors="replace")), safe_text, sess),
                )
                _insert_chunks(conn, aid, chunks)
        return aid
    except Exception:
        return None


def read_stored_output(archive_id: str, *, env: dict[str, str] | None = None) -> str | None:
    """Get the original stored text, including whitespace, for delta generation."""
    if not _ID_RE.fullmatch(archive_id):
        return None
    try:
        with closing(_connect(env)) as conn:
            row = conn.execute("SELECT content FROM blobs WHERE id = ?", (archive_id,)).fetchone()
            return row[0] if row else None
    except (OSError, sqlite3.Error):
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
        "hint": f"Query via {retrieval_hint(aid, 'query', env=env)}",
    }


def _fts_or_query(terms: list[str]) -> str:
    """Quoted-token OR query, so punctuation never causes an FTS5 syntax error."""
    return " OR ".join('"' + t.replace('"', "") + '"' for t in terms[:8])


def _ranked(
    conn: sqlite3.Connection, table: str, archive_id: str, terms: list[str], depth: int
) -> list[tuple[int, int, str]]:
    if not terms:
        return []
    try:
        return conn.execute(
            f"SELECT start_line, end_line, body FROM {table} "
            f"WHERE blob_id = ? AND {table} MATCH ? ORDER BY bm25({table}) LIMIT ?",
            (archive_id, _fts_or_query(terms), depth),
        ).fetchall()
    except sqlite3.OperationalError:
        return []


def rrf_merge(
    rankings: list[list[tuple[int, int, str]]], limit: int, k: int = _RRF_K
) -> list[tuple[int, int, str]]:
    """Reciprocal rank fusion: each chunk scores sum(1 / (k + rank)), ranks starting at 1."""
    scores: dict[tuple[int, int], float] = {}
    bodies: dict[tuple[int, int], str] = {}
    first_seen: dict[tuple[int, int], int] = {}
    for ranking in rankings:
        for rank, (s_ln, e_ln, body) in enumerate(ranking, start=1):
            key = (int(s_ln), int(e_ln))
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            bodies.setdefault(key, body)
            first_seen.setdefault(key, len(first_seen))
    keys = sorted(scores, key=lambda x: (-scores[x], first_seen[x]))
    return [(key[0], key[1], bodies[key]) for key in keys[:limit]]


def _search_chunks(conn: sqlite3.Connection, archive_id: str, query: str, limit: int) -> list[tuple[int, int, str]]:
    terms = [t for t in re.findall(r"[A-Za-z0-9_./:-]+", query) if len(t) >= 2]
    rankings: list[list[tuple[int, int, str]]] = []
    depth = max(limit * 4, 20)
    if _has_fts5(conn):
        rankings.append(_ranked(conn, "chunks_fts", archive_id, terms, depth))
    if _has_trigram(conn):
        # Trigram needs >= 3 characters per token to match anything.
        rankings.append(_ranked(conn, "chunks_tri", archive_id, [t for t in terms if len(t) >= 3], depth))
    hits = rrf_merge([r for r in rankings if r], limit)
    if hits:
        return hits
    table = "chunks_fts" if _has_fts5(conn) else "chunks_plain"
    return conn.execute(
        f"SELECT start_line, end_line, body FROM {table} WHERE blob_id = ? AND body LIKE ? LIMIT ?",
        (archive_id, f"%{query.strip()}%", limit),
    ).fetchall()


def suggest_correction(query: str, vocabulary: set[str]) -> str | None:
    """Swap each unknown query word for its closest indexed word; None when nothing changes."""
    if not vocabulary:
        return None
    lower_vocab: dict[str, str] = {}
    for w in sorted(vocabulary):
        lower_vocab.setdefault(w.lower(), w)
    changed = False
    out: list[str] = []
    for tok in re.split(r"(\s+)", query):
        if not tok or tok.isspace() or not _WORD_RE.fullmatch(tok) or tok.lower() in lower_vocab:
            out.append(tok)
            continue
        low = tok.lower()
        pool = [w for w in lower_vocab if abs(len(w) - len(low)) <= 2]
        best = difflib.get_close_matches(low, pool, n=1, cutoff=0.75)
        if best:
            out.append(lower_vocab[best[0]])
            changed = True
        else:
            out.append(tok)
    return "".join(out) if changed else None


def _archive_vocabulary(conn: sqlite3.Connection, archive_id: str) -> set[str]:
    row = conn.execute("SELECT content FROM blobs WHERE id = ?", (archive_id,)).fetchone()
    if row is None:
        return set()
    return set(_WORD_RE.findall(str(row[0] or "")[:_VOCAB_MAX_CHARS]))


def query_output(
    archive_id: str,
    query: str,
    *,
    limit: int = 5,
    env: dict[str, str] | None = None,
    require_hit: bool = False,
) -> str | None:
    """Search an archive: Porter + trigram FTS5 merged by reciprocal rank fusion.

    Falls back to a substring scan, then (unless `require_hit`) to one spelling
    correction drawn from the archive's own words.
    """
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
            hits = _search_chunks(conn, archive_id, query, limit)
            corrected: str | None = None
            if not hits and not require_hit:
                corrected = suggest_correction(query, _archive_vocabulary(conn, archive_id))
                if corrected:
                    hits = _search_chunks(conn, archive_id, corrected, limit)

            if not hits:
                if require_hit:
                    return None
                return (
                    f"[Terser Sandbox `{archive_id}` ({total_lines} lines, {total_bytes:,} B, cmd=`{cmd}`)]: "
                    f"no matches for `{query}`."
                )
            if corrected:
                head = (
                    f"[Terser Sandbox `{archive_id}`: no matches for `{query}`; "
                    f"{len(hits)} chunk(s) of {total_lines} lines for `{corrected}` (spelling corrected)]"
                )
            else:
                head = f"[Terser Sandbox `{archive_id}`: {len(hits)} matching chunk(s) of {total_lines} lines for `{query}`]"
            parts = [head]
            for s_ln, e_ln, body in hits:
                parts.append(f"--- L{s_ln}-L{e_ln} ---\n{body}")
            return "\n".join(parts)
    except Exception:
        return None


def purge_archives(
    *,
    archive_id: str | None = None,
    session_id: str | None = None,
    everything: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Delete archives by id (`--last` allowed), by session, or all of them.

    Raw Rust sidecars (`raw-archives/<id>.txt`) for the deleted ids are removed too.
    """
    if not (archive_id or session_id or everything):
        return {"error": "Provide `archive_id`, `session`, or `all: true`."}
    ids: list[str] = []
    if archive_id:
        resolved = _resolve_id(archive_id, env)
        if not resolved:
            return {"error": f"Invalid or unknown archive id `{archive_id}`."}
        ids.append(resolved)
    try:
        with closing(_connect(env)) as conn:
            if everything:
                ids = [str(r[0]) for r in conn.execute("SELECT id FROM blobs").fetchall()]
            elif session_id:
                ids.extend(
                    str(r[0]) for r in conn.execute(
                        "SELECT id FROM blobs WHERE session_id = ?", (str(session_id)[:80],)
                    ).fetchall()
                )
            ids = list(dict.fromkeys(ids))
            existing = [
                i for i in ids
                if conn.execute("SELECT 1 FROM blobs WHERE id = ?", (i,)).fetchone() is not None
            ]
            with conn:
                _delete_ids(conn, existing)
    except Exception as exc:
        return {"error": f"Purge failed: {type(exc).__name__}"}
    raw_deleted = 0
    rdir = _raw_archives_dir(env)
    if everything:
        targets = list(rdir.glob("*.txt")) if rdir.is_dir() else []
    else:
        targets = [rdir / f"{i}.txt" for i in ids]
    for p in targets:
        if not _ID_RE.match(p.stem):
            continue
        try:
            if p.is_file():
                p.unlink()
                raw_deleted += 1
        except OSError:
            continue
    return {"deleted": len(existing), "raw_deleted": raw_deleted, "ids": ids[:20]}


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


_RECALL_STOP = frozenset(
    "the a an and or to of in on for with is it this that be do does did can you me my we our please "
    "what how why when where which there their them then than from into fix add make use just also now".split()
)


def _recall_terms(text: str) -> list[str]:
    import re as _re

    out = []
    for t in _re.findall(r"[a-z0-9]{3,}", (text or "").lower()):
        if t in _RECALL_STOP:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if len(t) > len(suf) + 3 and t.endswith(suf):
                t = t[: -len(suf)]
                break
        out.append(t)
    return out


def recall_events(
    workspace: str,
    query: str,
    *,
    session_id: str = "",
    limit: int = 5,
    max_chars: int = 600,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Return logged edits/errors/commands that best match ``query`` (IDF-weighted term overlap).

    Used after compaction so a new prompt gets back the earlier facts it refers to.
    The current session's events rank first; other recent sessions in the workspace count too.
    """
    import math

    norm_ws = normalize_workspace(workspace)
    terms = set(_recall_terms(query))
    if not norm_ws or not terms or not _db_path(env).is_file():
        return []
    try:
        with closing(_connect(env)) as conn:
            rows = conn.execute(
                "SELECT session_id, kind, summary FROM session_events WHERE workspace = ? AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT 300",
                (norm_ws, time.time() - 72 * 3600),
            ).fetchall()
    except Exception:
        return []
    docs = [(sid, kind, str(summary or ""), set(_recall_terms(str(summary or "")))) for sid, kind, summary in rows]
    if not docs:
        return []
    df: dict[str, int] = {}
    for *_, toks in docs:
        for t in toks & terms:
            df[t] = df.get(t, 0) + 1
    n = len(docs)
    scored = []
    for rank, (sid, kind, summary, toks) in enumerate(docs):
        hit = toks & terms
        if not hit:
            continue
        score = sum(math.log(1 + n / df[t]) for t in hit)
        score *= 1.5 if session_id and sid == session_id[:80] else 1.0
        score *= 1.2 if kind == "error" else 1.0
        scored.append((-score, rank, kind, summary))
    scored.sort()
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for _, _, kind, summary in scored:
        if summary in seen:
            continue
        line = f"- {kind}: {summary}"
        if total + len(line) > max_chars or len(out) >= limit:
            break
        seen.add(summary)
        out.append(line)
        total += len(line) + 1
    return out


def heal_sqlite_db(db_path: Path) -> dict[str, Any]:
    """Lossless SQLite integrity check & auto-heal via VACUUM INTO + REINDEX + atomic swap."""
    if not db_path.is_file():
        return {"status": "missing", "path": str(db_path)}
    try:
        with closing(sqlite3.connect(str(db_path), timeout=2.0)) as conn:
            row = conn.execute("PRAGMA quick_check").fetchone()
            if row and str(row[0]).lower() == "ok":
                return {"status": "ok", "path": str(db_path)}
    except sqlite3.Error:
        pass

    tmp = db_path.with_suffix(db_path.suffix + f".heal.{os.getpid()}.tmp")
    try:
        if tmp.exists():
            tmp.unlink()
        uri = f"file:{db_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=2.0)) as src:
            src.execute(f"VACUUM INTO '{str(tmp).replace(chr(39), chr(39) * 2)}'")
        with closing(sqlite3.connect(str(tmp), timeout=2.0)) as dst:
            dst.execute("REINDEX")
            dst.execute("PRAGMA journal_mode=WAL")
            chk = dst.execute("PRAGMA quick_check").fetchone()
            if not chk or str(chk[0]).lower() != "ok":
                raise sqlite3.DatabaseError("healed db failed quick_check")
        for suffix in ("-wal", "-shm"):
            side = Path(str(db_path) + suffix)
            if side.exists():
                try:
                    side.unlink()
                except OSError:
                    pass
        os.replace(tmp, db_path)
        return {"status": "healed", "path": str(db_path)}
    except Exception as exc:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        bak = db_path.with_suffix(f"{db_path.suffix}.corrupt.{int(time.time())}.bak")
        try:
            os.replace(db_path, bak)
            return {"status": "quarantined", "path": str(db_path), "backup": str(bak), "error": str(exc)}
        except OSError:
            return {"status": "failed", "path": str(db_path), "error": str(exc)}
