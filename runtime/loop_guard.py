"""Terser — Unified Tiered Loop Guard & FormRepair.

Combines fast deterministic thrash detection with cross-session EnvFacts
FormRepair and loop guard, coordinated by a single per-session cooldown so two
nudges NEVER fire back-to-back on the same loop.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from runtime.config import atomic_write_json, ensure_vendor_paths, record_savings, state_home

_SID_RE = re.compile(r"^[0-9a-zA-Z_.-]{1,96}$")
_TTL_SECONDS = 7 * 86400
_STALE_SECONDS = 1800.0
_REPEAT_AFTER = 3
_LABEL_MAX = 80
_SHELL_EDIT_RE = re.compile(
    r"(?:"
    r"\bsed\s+(?:-[^\s]*i|--in-place)\b"
    r"|\bperl\s+-[^\s]*i"
    r"|\b(?:patch|apply_patch)\b"
    r"|\bgit\s+apply\b"
    r"|\btee\s+(?:-a\s+)?[^\s|&;]+"
    r"|(?:^|[\s;&|])>{1,2}\s*[^\s&|;]+"
    r")",
    re.IGNORECASE,
)


def _is_shell_edit(tool_name: str, args: dict[str, Any]) -> bool:
    low = (tool_name or "").lower()
    if low not in ("run_command", "bash", "shell", "exec_command", "local_shell", "powershell"):
        return False
    cmd = args.get("CommandLine") or args.get("command") or args.get("cmd")
    if isinstance(cmd, list):
        cmd = " ".join(str(x) for x in cmd)
    return isinstance(cmd, str) and bool(_SHELL_EDIT_RE.search(cmd))


def _state_dir(env: dict[str, str] | None = None) -> Path:
    return state_home(env) / "loop-state"


def _state_path(session_id: str, env: dict[str, str] | None = None) -> Path:
    safe = re.sub(r"[^0-9a-zA-Z_.-]", "_", session_id)[:80]
    return _state_dir(env) / f"{safe}.json"


def _clean_label(text: str) -> str:
    cleaned = "".join(ch if ch == " " or ch.isprintable() else " " for ch in str(text or ""))
    cleaned = " ".join(cleaned.split())
    return (cleaned[: _LABEL_MAX - 3] + "...") if len(cleaned) > _LABEL_MAX else cleaned


def _load(session_id: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    p = _state_path(session_id, env)
    try:
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save(session_id: str, state: dict[str, Any], now: float, env: dict[str, str] | None = None) -> None:
    sdir = _state_dir(env)
    try:
        if sdir.is_dir():
            cutoff = now - _TTL_SECONDS
            for p in sdir.glob("*.json"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    continue
    except OSError:
        pass
    atomic_write_json(_state_path(session_id, env), state)


def _tool_signature(tool_name: str, args: dict[str, Any]) -> tuple[str, str] | None:
    low = (tool_name or "").lower()
    if low in ("run_command", "bash", "shell", "exec_command", "local_shell", "powershell"):
        cmd = args.get("CommandLine") or args.get("command") or args.get("cmd")
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        if not isinstance(cmd, str) or not cmd.strip():
            return None
        norm = " ".join(cmd.split())
        h = hashlib.sha256(f"shell:{norm}".encode("utf-8")).hexdigest()[:20]
        return h, _clean_label(norm)
    if low in ("read_file", "view_file", "read", "edit_file", "write_file", "edit", "write"):
        raw_path = args.get("Path") or args.get("AbsolutePath") or args.get("file_path") or args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        norm_p = raw_path.lower() if os.name == "nt" else raw_path
        extra = ""
        if low in ("read_file", "view_file", "read"):
            extra = f":{args.get('StartLine') or args.get('offset')}:{args.get('EndLine') or args.get('limit')}"
        elif low in ("edit_file", "edit", "write_file", "write"):
            # Include a prefix of Target/old_string/Content so distinct edits to different
            # regions of the same file are never mistaken for an identical thrash loop.
            snippet = str(args.get("Target") or args.get("old_string") or args.get("Content") or args.get("content") or "")[:120]
            extra = ":" + hashlib.sha256(snippet.encode("utf-8", errors="ignore")).hexdigest()[:12]
        h = hashlib.sha256(f"{low}:{norm_p}{extra}".encode("utf-8")).hexdigest()[:20]
        return h, _clean_label(f"{tool_name}({Path(raw_path).name})")
    if low in ("grep", "glob", "call_mcp_tool") or low.startswith("mcp"):
        try:
            canon = json.dumps({"t": low, "a": args}, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        h = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:20]
        return h, _clean_label(tool_name)
    return None


def _should_fire(count: int, threshold: int) -> bool:
    return count >= threshold and ((count - threshold) % _REPEAT_AFTER == 0)


def record_tool_event(
    session_id: str,
    tool_name: str,
    args: dict[str, Any],
    *,
    is_error: bool = False,
    cfg: dict[str, Any],
    now: float | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Track streaks and return (and queue) a single crisp redirect if thrashing."""
    if not cfg.get("loop_guard", True) or cfg.get("shadow"):
        return None
    if not session_id or not _SID_RE.match(session_id):
        return None

    sig = _tool_signature(tool_name, args or {})
    if sig is None:
        return None
    key, label = sig
    now = time.time() if now is None else float(now)

    state = _load(session_id, env)
    if state.get("last_ts") and (now - float(state["last_ts"])) > _STALE_SECONDS:
        state = {}

    same_streak = int(state.get("same_streak") or 0) + 1 if state.get("last_key") == key else 1
    state["last_key"] = key
    state["same_streak"] = same_streak
    state["last_ts"] = now

    fail_streaks = state.get("fail_streaks") if isinstance(state.get("fail_streaks"), dict) else {}
    if is_error:
        fail_count = int(fail_streaks.get(key) or 0) + 1
        fail_streaks[key] = fail_count
    else:
        # Successful edits (including shell-based edits like sed -i / patch / git apply)
        # represent real progress and reset prior failure streaks so re-running tests is never falsely nudged.
        low_tool = (tool_name or "").lower()
        if low_tool in ("edit_file", "write_file", "edit", "write", "multiedit", "apply_patch") or _is_shell_edit(tool_name, args or {}):
            fail_streaks.clear()
        else:
            fail_streaks.pop(key, None)
        fail_count = 0
    if len(fail_streaks) > 50:
        fail_streaks = dict(list(fail_streaks.items())[-50:])
    state["fail_streaks"] = fail_streaks

    # Shared cooldown prevents duplicate nudges across mechanisms
    cooldown = float(cfg.get("nudge_cooldown_s", 45))
    last_nudge_ts = float(state.get("last_nudge_ts") or 0.0)
    in_cooldown = (now - last_nudge_ts) < cooldown

    fail_thr = int(cfg.get("fail_streak", 3))
    same_thr = int(cfg.get("thrash_streak", 3))
    nudge: str | None = None
    if not in_cooldown:
        if _should_fire(fail_count, fail_thr):
            nudge = (
                f"[Terser] `{label}` failed {fail_count}x in a row. "
                "Stop repeating it—diagnose the root error or switch approach."
            )
        elif _should_fire(same_streak, same_thr):
            nudge = (
                f"[Terser] `{label}` ran {same_streak}x identically. "
                "Verify what changed before running it again."
            )
        if nudge:
            state["pending_nudge"] = nudge
            state["last_nudge_ts"] = now

    _save(session_id, state, now, env)
    return nudge


def pop_pending_nudge(
    session_id: str,
    *,
    cfg: dict[str, Any],
    now: float | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Atomically pop any queued thrash/loop redirect (for PreInvocation)."""
    if not cfg.get("loop_guard", True) or cfg.get("shadow"):
        return None
    if not session_id or not _SID_RE.match(session_id):
        return None
    state = _load(session_id, env)
    nudge = state.get("pending_nudge")
    if not isinstance(nudge, str) or not nudge.strip():
        return None
    state.pop("pending_nudge", None)
    _save(session_id, state, time.time() if now is None else float(now), env)
    return nudge


def apply_form_repair(
    cwd: str,
    command: str,
    *,
    cfg: dict[str, Any],
    host: str = "antigravity",
    env: dict[str, str] | None = None,
) -> tuple[str, bool]:
    """Rewrite a leading broken shell command using confirmed Terser EnvFacts.

    Returns ``(command_out, was_repaired)``. Only rewrites exact leading token/prefix
    matches followed by whitespace or end-of-string, and never rewrites ``cd ... &&``
    repairs so working-directory semantics stay 100% intact.
    """
    if not cfg.get("form_repair", True) or cfg.get("shadow"):
        return command, False
    if not command or not command.strip() or not cwd:
        return command, False
    try:
        ensure_vendor_paths()
        from tokeneconomizer.microskills.envfacts import EnvFactsDistiller  # type: ignore
        from tokeneconomizer.plugin import paths as te_paths  # type: ignore
        from tokeneconomizer.plugin import repos as te_repos  # type: ignore

        e = env if env is not None else os.environ
        root = te_paths.state_root(e)
        repo = te_repos.resolve(cwd, root=root)
        mech_dir = os.path.join(te_paths.repo_dir(root, repo.key), "mech")
        if not os.path.isdir(mech_dir):
            return command, False
        dist = EnvFactsDistiller.load(repo.label, mech_dir)
    except Exception:
        return command, False

    stripped = command.lstrip()
    lead = command[: len(command) - len(stripped)]
    for fact in dist.facts.values():
        if getattr(fact, "family", "") != "form_repair" or not fact.injectable():
            continue
        bad = (fact.params.get("a") or "").strip()
        good = (fact.working_form() or "").strip()
        if not bad or not good or bad == good or good.startswith("cd "):
            continue
        if stripped == bad or (stripped.startswith(bad) and stripped[len(bad): len(bad) + 1].isspace()):
            repaired = lead + good + stripped[len(bad):]
            record_savings("form_repair", 2000, len(repaired), host=host, detail=f"{bad}->{good}", env=env)
            return repaired, True
    return command, False
