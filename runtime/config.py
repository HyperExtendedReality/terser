"""Unified configuration, path resolution, and feature flags for Terser.

All state lives locally under ``~/.terser`` (override via ``TERSER_HOME``).
Zero network calls, zero telemetry, pure Python stdlib.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = PLUGIN_ROOT / "vendor"
TE_VENDOR_DIR = VENDOR_DIR / "tokeneconomizer"
TO_VENDOR_DIR = VENDOR_DIR / "token_optimizer"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    "shadow": False,
    # PreToolUse Shell pipeline: FormRepair -> Search Intercept -> Output Compression
    "form_repair": True,
    "bash_compress": True,
    "cmd_delta": True,             # Cross-run delta-diff on repeated test/lint/build/git runs
    "fts_sandbox": True,           # Index oversized outputs in SQLite FTS5 (`terse query <id>`)
    "auto_approve_readonly": False,
    "enhanced_compression": True,  # ANSI/spinner strip + stream filters + duplicate collapse + JSON fold
    # PreToolUse Read Funnel (4-tier quality-preserving)
    "read_cache": True,
    "delta_diff": True,
    "outline_first": True,         # First unranged read of large code/doc files returns outline once
    "outline_min_bytes": 10240,    # 10 KB threshold so small/medium files pass untouched
    "outline_min_lines": 250,      # 250-line minimum threshold
    "outline_backoff_limit": 2,    # Auto-backoff outline_first if agent overrides 2x in a session
    "mcp_refetch_guard": True,
    "mcp_dedup_window_s": 120,
    # AST code-index integration
    "index_auto_reindex": True,
    "index_grep_answer": True,     # Answer huge Greps (>=66 lines) directly from fresh index once
    "index_surface": "counter",    # "counter" (~1k tok) | "core" (~4k tok) | "full" (~26k tok)
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
    # Tool pruning (Claude Code & Codex)
    "toolprune": "propose",           # "propose" | "auto" | "off"
    # PostToolUse oversized output archival (Claude Code & Codex)
    "archive_large_outputs": True,
    "archive_threshold_bytes": 16384,
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
    ):
        v = env.get(env_key)
        if v is not None and v.strip():
            if _falsy(v):
                cfg[cfg_key] = False
            elif _truthy(v):
                cfg[cfg_key] = True
    return cfg


def save_config(cfg: dict[str, Any], env: dict[str, str] | None = None) -> Path:
    p = config_path(env)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cfg.", suffix=".tmp", dir=str(p.parent))
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
    for d in (str(VENDOR_DIR), str(TO_VENDOR_DIR)):
        if os.path.isdir(d) and d not in sys.path:
            sys.path.append(d)


def normalize_workspace(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("file://"):
        text = text[len("file://"):]
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
        fd, tmp = tempfile.mkstemp(prefix=".terser.", suffix=".tmp", dir=str(path.parent))
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
