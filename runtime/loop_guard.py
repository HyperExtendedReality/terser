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
    """Single-line printable label, capped at ``_LABEL_MAX`` chars with an ellipsis."""
    words = "".join(c if c.isprintable() else " " for c in str(text or "")).split()
    label = " ".join(words)
    if len(label) <= _LABEL_MAX:
        return label
    return label[: _LABEL_MAX - 3] + "..."


def clear_session_state(session_id: str, env: dict[str, str] | None = None) -> None:
    if not session_id:
        return
    try:
        _state_path(session_id, env).unlink(missing_ok=True)
    except OSError:
        pass


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
        elif low in ("edit_file", "edit", "write_file", "write", "replace_file_content", "multi_replace_file_content"):
            # Hash the full target + replacement/content so distinct edits sharing a boilerplate
            # prefix never collide as an identical thrash loop.
            part_a = str(
                args.get("Target") or args.get("TargetContent") or args.get("old_string")
                or args.get("Content") or args.get("content") or ""
            )
            part_b = str(args.get("Replacement") or args.get("ReplacementContent") or args.get("new_string") or "")
            extra = ":" + hashlib.sha256(f"{part_a}\0{part_b}".encode("utf-8", errors="ignore")).hexdigest()[:16]
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


FORM_REPAIR_GATE_FILE = "form-repair-gate.json"
_FORM_REPAIR_GATE_MAX = 256
FORM_REPAIR_GATE_TTL_S = 600.0  # Bounded staleness for anything the stat signatures cannot see


def _stat_sig(path: str) -> list[int] | None:
    """``[mtime_ns, size]`` of a file, or None when it does not exist (mirrored by terser-fast)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [int(st.st_mtime_ns), int(st.st_size)]


def _record_form_repair_gate(
    cwd: str,
    host: str,
    facts_path: str,
    candidates: list[tuple[str, str]],
    e: Any,
    env: dict[str, str] | None,
) -> None:
    """Remember whether this checkout has any usable FormRepair facts.

    terser-fast reads this to answer Bash PreToolUse without starting Python when the
    checkout provably has nothing to repair. The entry is only trusted while every input
    that decided it (the facts file, the checkout's ``.git/config`` that keys the repo
    identity, and the TokenEconomizer env overrides) is byte-for-byte unchanged, so the
    decision is identical to running this function. Linked worktrees (``.git`` is a file)
    and checkouts outside git are never recorded and always take the full path.
    """
    if host != "claude":
        return
    try:
        from tokeneconomizer.plugin import paths as te_paths  # type: ignore

        root = te_paths.find_repo_root(cwd, e)
        if not root:
            return  # A later `git init` above cwd would change the identity; never cache.
        dot_git = os.path.join(root, ".git")
        if not os.path.isdir(dot_git):
            return
        git_config = os.path.join(dot_git, "config")
        gate_path = state_home(env) / FORM_REPAIR_GATE_FILE
        try:
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
            if not isinstance(gate, dict):
                gate = {}
        except (OSError, ValueError):
            gate = {}
        entry = {
            "facts": facts_path,
            "facts_sig": _stat_sig(facts_path),
            "git_config": git_config,
            "git_sig": _stat_sig(git_config),
            "te_home": str(e.get("TOKENECONOMIZER_HOME") or ""),
            "te_repo": str(e.get("TOKENECONOMIZER_REPO") or ""),
            "ceilings": str(e.get("GIT_CEILING_DIRECTORIES") or ""),
            "home": str(e.get("HOME") or ""),
            "has": bool(candidates),
        }
        key = f"{host}|{cwd}"
        prev = gate.get(key)
        if (
            isinstance(prev, dict)
            and {k: v for k, v in prev.items() if k != "at"} == entry
            and time.time() - float(prev.get("at") or 0) < FORM_REPAIR_GATE_TTL_S / 2
        ):
            return
        entry["at"] = time.time()
        gate.pop(key, None)
        gate[key] = entry
        if len(gate) > _FORM_REPAIR_GATE_MAX:
            for old in list(gate)[: len(gate) - _FORM_REPAIR_GATE_MAX]:
                gate.pop(old, None)
        atomic_write_json(gate_path, gate)
    except Exception:
        return


_PLAIN_EXE_RE = re.compile(r"^[A-Za-z0-9_.+\-]+$")


def _is_plain_executable(form: str) -> bool:
    """True for a bare command name such as ``python3`` or ``py.exe`` (no paths, args, or shell syntax)."""
    return bool(_PLAIN_EXE_RE.match(form))


def apply_form_repair(
    cwd: str,
    command: str,
    *,
    cfg: dict[str, Any],
    host: str = "antigravity",
    env: dict[str, str] | None = None,
    shell_neutral_only: bool = False,
) -> tuple[str, bool]:
    """Rewrite a leading broken shell command using confirmed Terser EnvFacts.

    ``shell_neutral_only`` limits repairs to plain executable-name swaps (one token, no shell
    syntax) so facts learned from POSIX shells never inject POSIX-only syntax into PowerShell.

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
        from tokeneconomizer.microskills.envfacts import STATE_SUBDIR, EnvFactsDistiller  # type: ignore
        from tokeneconomizer.names import repo_filename  # type: ignore
        from tokeneconomizer.plugin import paths as te_paths  # type: ignore

        e = env if env is not None else os.environ
        repo = te_paths.repo_identity(cwd, e)
        mech_dir = os.path.join(te_paths.repo_state_dir(repo, host, e), "mechanisms")
        facts_path = os.path.join(mech_dir, STATE_SUBDIR, repo_filename(repo.label) + ".json")
        if not os.path.isdir(mech_dir):
            _record_form_repair_gate(cwd, host, facts_path, [], e, env)
            return command, False
        dist = EnvFactsDistiller.load(repo.label, mech_dir)
    except Exception:
        return command, False

    candidates: list[tuple[str, str]] = []
    for fact in dist.facts.values():
        if getattr(fact, "family", "") != "form_repair" or not fact.injectable():
            continue
        bad = (fact.params.get("a") or "").strip()
        good = (fact.working_form() or "").strip()
        if not bad or not good or bad == good or good.startswith("cd "):
            continue
        candidates.append((bad, good))
    _record_form_repair_gate(cwd, host, facts_path, candidates, e, env)
    if shell_neutral_only:
        candidates = [(b, g) for b, g in candidates if _is_plain_executable(b) and _is_plain_executable(g)]

    stripped = command.lstrip()
    lead = command[: len(command) - len(stripped)]
    for bad, good in candidates:
        if stripped == bad or (stripped.startswith(bad) and stripped[len(bad): len(bad) + 1].isspace()):
            repaired = lead + good + stripped[len(bad):]
            if host == "codex":
                from runtime import codex_hooks
                if not codex_hooks.equivalent_command(command, repaired, cwd, env):
                    continue
            record_savings("form_repair", 2000, len(repaired), host=host, detail=f"{bad}->{good}", env=env)
            return repaired, True
    return command, False
