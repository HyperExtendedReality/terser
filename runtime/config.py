"""Unified configuration, path resolution, and feature flags for Terser.

All state lives locally under ``~/.terser`` (override via ``TERSER_HOME``).
Zero network calls, zero telemetry, pure Python stdlib.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = PLUGIN_ROOT / "vendor"
TE_VENDOR_DIR = VENDOR_DIR / "tokeneconomizer"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "shadow": False,
    # PreToolUse Shell pipeline: FormRepair -> Search Intercept -> Output Compression
    "form_repair": True,
    "codex_form_repair": False,     # Opt in to rewriting confirmed failed command forms
    "bash_compress": True,
    "cmd_delta": True,             # Cross-run delta-diff on repeated test/lint/build/git runs
    "fts_sandbox": True,           # Index oversized outputs in SQLite FTS5 (`terse query <id>`)
    "auto_approve_readonly": False,
    "enhanced_compression": True,  # ANSI/spinner strip + stream filters + duplicate collapse + JSON fold
    # PreToolUse Read Funnel (4-tier quality-preserving)
    "read_cache": True,
    "delta_diff": True,
    "symbol_delta": True,          # Big change confined to a few symbols -> return just those regions
    "edit_miss_hints": True,       # Failed Edit -> exact closest region / ambiguous match lines
    "search_repeat_elision": True, # Identical Glob/Grep result repeated in-session -> short pointer once
    "outline_first": True,        # First unranged read of large code/doc files returns outline once
    "outline_min_bytes": 10240,    # 10 KB threshold so small/medium files pass untouched
    "outline_min_lines": 250,      # 250-line minimum threshold
    "outline_backoff_limit": 2,    # Auto-backoff outline_first if agent overrides 2x in a session
    "mcp_refetch_guard": True,
    "codex_mcp_refetch_guard": False,  # Opt in to same-turn reuse of verified static results
    "mcp_dedup_window_s": 120,
    # AST code-index integration
    "index_auto_reindex": True,
    "index_grep_answer": True,     # Answer huge Greps (>=66 lines) directly from fresh index once
    "index_surface": "counter",    # "counter" (~1k tok) | "core" (~4k tok) | "full" (~26k tok)
    "index_auto_init": True,       # SessionStart: background-index unindexed git repos (no /command needed)
    "index_auto_init_max_files": 5000,
    # Zero-command automation (no slash commands required)
    "health_check": True,          # SessionStart: user-only warning when Terser is broken
    "intent_hints": True,          # Plain-language Terser questions -> exact tool call injected
    "session_summary": True,       # Stop: one user-only savings line per session
    # Unified Loop & Thrash Guard (deduped single-nudge cooldown)
    "loop_guard": True,
    "thrash_streak": 3,
    "fail_streak": 3,
    "nudge_cooldown_s": 45,
    # Unified SessionStart Brief (prompt-cache-safe, strictly budget-capped)
    "session_brief": True,
    "session_brief_max_chars": 1600,  # ~400 tokens hard cap (strictly byte-stable)
    "repo_map": True,                 # Byte-stable PageRank fallback when AST index is absent
    "repo_map_max_tokens": 300,
    "code_discipline": "minimal",     # "minimal" (7-rung minimalism, never cuts safety) | "off"
    "microskills": "envfacts",        # "envfacts" (LLM-free, fast) | "on" | "off"
    "continuity_restore": True,
    "context_fill_nudges": True,
    "compact_recall": True,
    "lean_replies": True,
    "fanout_checkpoint": True,
    "python_worker": True,             # Warm hook worker for delegated events (same dispatch, no per-call interpreter start)
    "subagent_model_routing": False,  # Opt-in: auto-route low-complexity research subagents to a lighter (lower-quality) model tier
    "subagent_verify": True,  # Zero-token double check of low-cost routed subagents (+ verify contract in their prompt)
    "subagent_cheap_model": "haiku",  # Claude Code low-cost tier for routed lookup subagents
    "subagent_cheap_effort": "low",  # Reasoning effort for routed subagents: low|medium|high|xhigh|max, "" = inherit
    "subagent_cheap_model_antigravity": "flash",  # Antigravity 2.0 subagent tier: flash_lite|flash|pro
    "subagent_cheap_effort_antigravity": "low",  # Antigravity 2.0 routing effort guidance / swarm effort: low|medium|high|max, "" = inherit
    # Tool pruning (Claude Code & Codex)
    "toolprune": "propose",           # "propose" | "auto" | "off"
    # PostToolUse oversized output archival (Claude Code & Codex)
    "archive_large_outputs": True,
    "archive_threshold_bytes": 16384,
    # User-tunable surfaces
    "contextignore": True,             # Deny reads of files matching .contextignore (project + ~/.terser)
    "json_lossy": False,               # Opt-in: drop nulls/empties/trace ids, cut long strings (original archived)
    "web_archive": True,               # Archive large WebFetch pages into sandbox.db and serve an outline + head
    "web_archive_threshold": 12000,    # Characters of compacted page text before archiving kicks in
    "code_mode": True,                 # terser-index `run_script` / `code_*` actions (local scripts, only prints return)
}


def state_home(env: dict[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    raw = (env.get("TERSER_HOME") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw))
    return Path.home() / ".terser"


def config_path(env: dict[str, str] | None = None) -> Path:
    return state_home(env) / "config.json"


def _truthy(val: Any) -> bool:
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


def _falsy(val: Any) -> bool:
    return str(val or "").strip().lower() in ("0", "false", "no", "off")


def load_config(env: dict[str, str] | None = None) -> dict[str, Any]:
    """Load merged defaults + ~/.terser/config.json + env overrides."""
    env = env if env is not None else os.environ
    cfg = dict(DEFAULT_CONFIG)
    p = config_path(env)
    try:
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    # Explicit environment overrides (highest precedence for testing/toggling)
    if _truthy(env.get("TERSER_DISABLE")) or _truthy(env.get("TE_SIDECALL")):
        cfg["enabled"] = False
    if _truthy(env.get("TERSER_SHADOW")):
        cfg["shadow"] = True
    for env_key, cfg_key in (
        ("TERSER_BASH_COMPRESS", "bash_compress"),
        ("TERSER_FORM_REPAIR", "form_repair"),
        ("TERSER_READ_CACHE", "read_cache"),
        ("TERSER_OUTLINE_FIRST", "outline_first"),
        ("TERSER_MCP_REFETCH_GUARD", "mcp_refetch_guard"),
        ("TERSER_LOOP_GUARD", "loop_guard"),
        ("TERSER_AUTO_APPROVE_READONLY", "auto_approve_readonly"),
        ("TERSER_AUTO_REINDEX", "index_auto_reindex"),
        ("TERSER_GREP_ANSWER", "index_grep_answer"),
        ("TERSER_SESSION_BRIEF", "session_brief"),
        ("TERSER_SUBAGENT_ROUTING", "subagent_model_routing"),
        ("TERSER_SUBAGENT_VERIFY", "subagent_verify"),
        ("TERSER_PYTHON_WORKER", "python_worker"),
        ("TERSER_AUTO_INDEX", "index_auto_init"),
        ("TERSER_HEALTH_CHECK", "health_check"),
        ("TERSER_INTENT_HINTS", "intent_hints"),
        ("TERSER_SESSION_SUMMARY", "session_summary"),
        ("TERSER_CONTEXTIGNORE", "contextignore"),
        ("TERSER_JSON_LOSSY", "json_lossy"),
        ("TERSER_WEB_ARCHIVE", "web_archive"),
        ("TERSER_CODE_MODE", "code_mode"),
    ):
        v = env.get(env_key)
        if v is not None and v.strip():
            if _falsy(v):
                cfg[cfg_key] = False
            elif _truthy(v):
                cfg[cfg_key] = True
    return cfg


_O_TMPFILE_FLAGS = (
    os.O_RDWR | os.O_CREAT | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NOINHERIT", 0) | getattr(os, "O_BINARY", 0)
)


def _mkstemp(*, prefix: str, suffix: str, dir: str) -> tuple[int, str]:
    """``tempfile.mkstemp`` equivalent (same flags, 0600, exclusive create) without importing
    ``tempfile``/``shutil``/``random`` (~30ms of startup for every hook and the MCP server)."""
    for _ in range(100):
        name = os.path.join(dir, f"{prefix}{os.urandom(6).hex()}{suffix}")
        try:
            return os.open(name, _O_TMPFILE_FLAGS, 0o600), name
        except FileExistsError:
            continue
    raise FileExistsError(f"no usable temporary name in {dir}")


def save_config(cfg: dict[str, Any], env: dict[str, str] | None = None) -> Path:
    p = config_path(env)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _mkstemp(prefix=".cfg.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, str(p))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def ensure_vendor_paths() -> None:
    """Put vendored stdlib engines on sys.path (idempotent, fast)."""
    for d in (str(VENDOR_DIR),):
        if os.path.isdir(d) and d not in sys.path:
            sys.path.append(d)


def normalize_workspace(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("file://"):
        text = text[len("file://"):]
    if "%" in text:  # unquote() is the identity without '%'; skip importing urllib (~20ms)
        import urllib.parse

        text = urllib.parse.unquote(text)
    if len(text) >= 3 and text[0] == "/" and text[2] == ":" and text[1].isalpha():
        text = text[1:]
    text = text.replace("\\", "/").rstrip("/")
    return text.lower() if (os.name == "nt" or (len(text) >= 2 and text[1] == ":")) else text


def workspace_hash(workspace: str) -> str:
    norm = normalize_workspace(workspace)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12] if norm else ""


def estimate_tokens(text: str) -> int:
    """Fast stdlib token estimator (~4 chars/token)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def atomic_write_json(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = _mkstemp(prefix=".terser.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
            os.replace(tmp, str(path))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        pass


_SAVINGS_MAX_BYTES = 5 * 1024 * 1024  # 5 MB rotation cap
_SAVINGS_KEEP_LINES = 10000


def _maybe_rotate_savings(log: Path) -> None:
    try:
        if not log.is_file() or log.stat().st_size <= _SAVINGS_MAX_BYTES:
            return
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= _SAVINGS_KEEP_LINES:
            return
        tail = "\n".join(lines[-_SAVINGS_KEEP_LINES:]) + "\n"
        fd, tmp = _mkstemp(prefix=".sav.", suffix=".tmp", dir=str(log.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(tail)
            os.replace(tmp, str(log))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        pass


def record_savings(
    category: str,
    raw_chars: int,
    served_chars: int,
    *,
    host: str = "unknown",
    detail: str = "",
    env: dict[str, str] | None = None,
) -> None:
    """Append a lightweight local-only savings event to ~/.terser/savings.jsonl."""
    saved_chars = max(0, int(raw_chars) - int(served_chars))
    if saved_chars <= 0:
        return
    try:
        import time
        log = state_home(env) / "savings.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        _maybe_rotate_savings(log)
        entry = {
            "ts": round(time.time(), 1),
            "host": host,
            "cat": category,
            "raw_tok": max(1, raw_chars // 4),
            "served_tok": max(0, served_chars // 4),
            "saved_tok": max(1, saved_chars // 4),
            "detail": detail[:100],
        }
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


def record_funnel_event(
    tier: str,
    event: str,
    *,
    host: str = "unknown",
    env: dict[str, str] | None = None,
) -> None:
    """Append a funnel tier event (`served` = short answer given, `retry` = model asked again).

    A high retry ratio for a tier means its short answers cost extra round-trips,
    which is the signal used to tune thresholds without guessing about quality.
    """
    try:
        import time
        log = state_home(env) / "funnel.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            if log.stat().st_size > 4 * 1024 * 1024:
                log.replace(log.with_suffix(".jsonl.1"))
        except OSError:
            pass
        entry = {"ts": round(time.time(), 1), "host": host, "tier": tier[:32], "ev": event[:16]}
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        pass


def load_funnel_summary(env: dict[str, str] | None = None) -> dict[str, dict[str, int]]:
    """Per-tier `served` / `retry` counts from ~/.terser/funnel.jsonl."""
    out: dict[str, dict[str, int]] = {}
    log = state_home(env) / "funnel.jsonl"
    try:
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict):
                    continue
                tier = str(row.get("tier") or "")
                ev = str(row.get("ev") or "")
                if tier and ev in ("served", "retry", "mismatch"):
                    bucket = out.setdefault(tier, {"served": 0, "retry": 0, "mismatch": 0})
                    bucket[ev] += 1
    except OSError:
        pass
    return out


# Funnel tiers whose short answers can be switched off (the read then passes through in full).
BACKOFF_TIERS = ("t0", "t0b", "t1", "t1sym", "t2")
_BACKOFF_FILE = "funnel-backoff.json"
_BACKOFF_REFRESH_S = 600
_BACKOFF_WINDOW_S = 7 * 86400
_BACKOFF_TAIL_BYTES = 256 * 1024
_BACKOFF_MIN_SERVED = 12
_BACKOFF_MAX_RETRY_RATIO = 0.5
_backoff_memo: dict[str, Any] = {}


def compute_tier_backoff(env: dict[str, str] | None = None, now: float | None = None) -> dict[str, list[str]]:
    """Per host, the funnel tiers whose recent retry ratio shows they cost more round-trips than they save.

    A tier backs off when, over the last 7 days, it served >= 12 short answers and more than half
    were immediately retried. Backing off only ever returns the full content, so it cannot lower
    answer quality. Old events age out of the window, which re-enables the tier.
    """
    import time

    now = time.time() if now is None else now
    counts: dict[tuple[str, str], list[int]] = {}
    log = state_home(env) / "funnel.jsonl"
    try:
        with open(log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _BACKOFF_TAIL_BYTES))
            data = fh.read()
        if size > _BACKOFF_TAIL_BYTES:
            data = data.split(b"\n", 1)[-1]  # drop the partial first line
        for line in data.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("tier") not in BACKOFF_TIERS:
                continue
            ts = row.get("ts")
            if not isinstance(ts, (int, float)) or now - ts > _BACKOFF_WINDOW_S:
                continue
            ev = row.get("ev")
            if ev not in ("served", "retry"):
                continue
            c = counts.setdefault((str(row.get("host") or "unknown"), row["tier"]), [0, 0])
            c[0 if ev == "served" else 1] += 1
    except OSError:
        pass
    out: dict[str, list[str]] = {}
    for (host, tier), (served, retry) in sorted(counts.items()):
        if served >= _BACKOFF_MIN_SERVED and retry / served > _BACKOFF_MAX_RETRY_RATIO:
            out.setdefault(host, []).append(tier)
    return out


def tier_backed_off(tier: str, host: str, env: dict[str, str] | None = None) -> bool:
    """True when `tier` is currently backed off for `host` (recomputed at most every 10 minutes)."""
    import time

    if tier not in BACKOFF_TIERS:
        return False
    path = state_home(env) / _BACKOFF_FILE
    now = time.time()
    memo = _backoff_memo.get(str(path))
    if memo and now - memo[0] < _BACKOFF_REFRESH_S:
        return tier in memo[1].get(host, ())
    disabled: dict[str, list[str]] | None = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        at = data.get("at") if isinstance(data, dict) else None
        if isinstance(at, (int, float)) and 0 <= now - at < _BACKOFF_REFRESH_S and isinstance(data.get("disabled"), dict):
            disabled = data["disabled"]
            memo_at = float(at)
    except (OSError, ValueError):
        pass
    if disabled is None:
        disabled = compute_tier_backoff(env, now)
        memo_at = now
        try:
            atomic_write_json(path, {"at": round(now, 1), "disabled": disabled})
        except Exception:
            pass
    _backoff_memo[str(path)] = (memo_at, disabled)
    return tier in disabled.get(host, ())


def no_window_kwargs() -> dict[str, Any]:
    """Subprocess kwargs that prevent console windows from flashing on Windows."""
    if sys.platform == "win32":
        import subprocess

        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


def detached_spawn_kwargs(*, breakaway: bool = False) -> dict[str, Any]:
    """Subprocess kwargs for detached background workers without console window flashes.

    Note: Never combine DETACHED_PROCESS with CREATE_NO_WINDOW on Windows — Windows
    ignores CREATE_NO_WINDOW when DETACHED_PROCESS is set, leaving the worker without
    a console so every child console process it spawns flashes a visible terminal window.
    CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP gives the worker a hidden console that
    all child processes inherit silently.
    """
    if sys.platform == "win32":
        import subprocess

        flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        if breakaway:
            flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
        return {"creationflags": flags}
    return {"start_new_session": True}


def windowless_python(executable: str | None = None) -> str:
    """Return pythonw.exe beside `executable` on Windows if present, else `executable`."""
    exe = executable or sys.executable
    if sys.platform != "win32" or not exe:
        return exe
    folder, name = os.path.split(exe)
    candidates = ["pythonw.exe"]
    if name.lower().startswith("python") and not name.lower().startswith("pythonw"):
        candidates.insert(0, "pythonw" + name[len("python"):])
    for cand in candidates:
        path = os.path.join(folder, cand)
        if os.path.isfile(path):
            return path
    return exe


def console_python(executable: str | None = None) -> str:
    """Return console python.exe beside `executable` on Windows (never pythonw.exe).

    Wrapped shell commands must use a console subsystem binary so PowerShell/cmd
    waits for completion and captures stdout/stderr instead of silently detaching.
    """
    exe = executable or sys.executable
    if sys.platform != "win32" or not exe:
        return exe
    folder, name = os.path.split(exe)
    low = name.lower()
    if not low.startswith("pythonw"):
        return exe
    candidates = ["python" + name[len("pythonw"):], "python.exe"]
    for cand in candidates:
        path = os.path.join(folder, cand)
        if os.path.isfile(path):
            return path
    return exe
