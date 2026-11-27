#!/usr/bin/env python3
"""Terser — Single-Process Multi-Host Lifecycle Hook Dispatcher.

Handles Antigravity 2.0, Claude Code, and Codex through ONE fast, fail-open Python
process per lifecycle event — eliminating multi-hook spawn latency and resolving
all lifecycle hook conflicts in Terser:

  PreToolUse (Shell):   FormRepair FIRST -> Bash Output Compression SECOND (single rewrite)
  PreToolUse (Read):    4-Tier Read Funnel (Dedup -> Delta Diff -> Outline-First -> Passthrough)
  PreToolUse (Grep):    Index-backed large Grep answering (Claude/Codex)
  PreToolUse (MCP):     120s Read-Only MCP Refetch Guard
  PostToolUse:          Edit cache invalidation + AST auto-reindex + Output compression
                        + Unified deduped Loop/Thrash Guard
  SessionStart:         ONE budget-capped (<=350 tok) Prompt-Cache-Safe Brief
  PreInvocation/Prompt: Pending loop-guard redirect + live context-fill nudges
  PreCompact/PostCmp:   Smart checkpoint capture & read-cache reset
  Stop/SessionEnd:      Lease-debounced detached rollup + session micro-skills fold

Fail-open contract: always exits 0, never corrupts stdout, never blocks the agent on error.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _THIS_DIR.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from runtime import cmd_delta, codex_hooks, index_bridge, loop_guard, output_compressor, read_funnel, session_brief
from runtime.config import (
    atomic_write_json,
    ensure_vendor_paths,
    load_config,
    record_funnel_event,
    record_savings,
    state_home,
)

_STDIN_MAX_BYTES = 5 * 1024 * 1024  # 5 MB safety cap
_VOLATILE_RESULT_KEY_RE = re.compile(r"(?i)duration|elapsed|took|timing|^ms$|timestamp")
_ROLLUP_LEASE_SECONDS = 30.0


def _enforce_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _parse_payload_bytes(raw: bytes) -> dict[str, Any]:
    try:
        if not raw or len(raw) > _STDIN_MAX_BYTES:
            return {}
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _read_stdin_json() -> dict[str, Any]:
    try:
        buf = getattr(sys.stdin, "buffer", None)
        if buf is not None:
            return _parse_payload_bytes(buf.read(_STDIN_MAX_BYTES + 1))
        text = sys.stdin.read(_STDIN_MAX_BYTES + 1)
        if not text or len(text) > _STDIN_MAX_BYTES:
            return {}
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _detect_host(payload: dict[str, Any], env: dict[str, str], hint: str | None) -> str:
    if hint in ("antigravity", "claude", "codex"):
        return hint
    if payload.get("conversationId") or "toolCall" in payload or "workspacePaths" in payload:
        return "antigravity"
    tp = str(payload.get("transcript_path") or "").replace("\\", "/")
    if "turn_id" in payload or "/.codex/" in tp or tp.rsplit("/", 1)[-1].startswith("rollout-"):
        return "codex"
    if env.get("ANTIGRAVITY_AGENT") == "1" or env.get("ANTIGRAVITY_CONVERSATION_ID"):
        return "antigravity"
    if env.get("CODEX_THREAD_ID") or env.get("CODEX_HOME"):
        return "codex"
    return "claude"


_SUBAGENT_BRIEF_AG = (
    "[Terser Subagent] Prefer `terser-index` (`order`/`route`, if available) or ranged reads (`StartLine`/`EndLine`); "
    "batch parallel reads; scope CLI output; return a concise summary with exact file paths and line spans; quote code verbatim only when the task asks for it."
)
_SUBAGENT_BRIEF_CC = (
    "[Terser Subagent] Prefer `terser-index` (`order`/`route`, if available) or ranged reads (`offset`/`limit`); "
    "batch parallel reads; scope CLI output; return a concise summary with exact file paths and line spans; quote code verbatim only when the task asks for it."
)
_SKIP_AG_SUBAGENT_TYPES = frozenset({"browser", "image-generator"})


def _sanitize_sid_part(raw: str) -> str:
    return "".join(c if (c.isascii() and (c.isalnum() or c in "_.-")) else "_" for c in raw)


def _session_id(payload: dict[str, Any], host: str) -> str:
    if host == "antigravity":
        base = str(payload.get("conversationId") or "").strip()
    else:
        base = str(payload.get("session_id") or "").strip()
    agent = ""
    for key in ("agent_id", "subagent_id", "agentId"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            agent = v.strip()
            break
    if base and agent and agent != base:
        b = _sanitize_sid_part(base)
        a = _sanitize_sid_part(agent)
        composed = f"{b}--{a}"
        if len(composed) > 78:
            composed = f"{b[:44]}--{a[:32]}"
        return composed
    return base


def _workspace(payload: dict[str, Any], env: dict[str, str]) -> str:
    ws = payload.get("workspacePaths")
    if isinstance(ws, list):
        for item in ws:
            if isinstance(item, str) and item.strip():
                return item.strip()
    for k in ("cwd",):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return env.get("CLAUDE_PROJECT_DIR") or os.getcwd()


# Claude Code / Codex shell tools whose stdout/stderr get lossless compression.
_POWERSHELL_TOOL_NAMES = ("PowerShell", "powershell")
_SHELL_TOOL_NAMES = ("Bash", "bash", "shell", "exec_command") + _POWERSHELL_TOOL_NAMES


# ---------------------------------------------------------------------------
# Shell Command Compression Rewrite Helpers (Antigravity PreToolUse)
# ---------------------------------------------------------------------------

def _ps_quote(token: str) -> str:
    return "'" + token.replace("'", "''") + "'"


def _split_powershell_command(command: str) -> list[str] | None:
    """Split a simple PowerShell command line into argv.

    Supports bare words and '...' / "..." quoting with doubled-quote escapes. Returns
    None for an unterminated quote or an empty command.
    """
    token_re = re.compile(r"""'(?:[^']|'')*'|"(?:[^"]|"")*"|[^\s'"]+""")
    argv: list[str] = []
    pos = 0
    current: list[str] = []
    while pos < len(command):
        if command[pos].isspace():
            if current:
                argv.append("".join(current))
                current = []
            pos += 1
            continue
        m = token_re.match(command, pos)
        if not m:
            return None  # unterminated quote
        piece = m.group(0)
        if piece[0] in "'\"" and len(piece) >= 2 and piece[-1] == piece[0]:
            q = piece[0]
            piece = piece[1:-1].replace(q + q, q)
        current.append(piece)
        pos = m.end()
    if current:
        argv.append("".join(current))
    return argv or None


def _split_safe_and_chain(command: str) -> list[str] | None:
    """Split top-level `&&` chains outside quotes (2-4 segments) for safe per-segment wrapping."""
    if "&&" not in command:
        return None
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            i += 1
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
        elif ch == "&" and i + 1 < n and command[i + 1] == "&":
            seg = "".join(buf).strip()
            if not seg:
                return None
            parts.append(seg)
            buf = []
            i += 2
        else:
            buf.append(ch)
            i += 1
    if quote is not None:
        return None
    tail = "".join(buf).strip()
    if not tail:
        return None
    parts.append(tail)
    return parts if 2 <= len(parts) <= 4 else None


def _wrap_command_for_compression(
    command: str,
    cfg: dict[str, Any],
    *,
    session_id: str = "",
) -> tuple[str | None, bool]:
    """If `command` is safe & whitelisted (or a safe `&&` chain of whitelisted commands), wrap it.

    Returns ``(rewritten_command_or_None, is_auto_approvable_readonly)``.
    """
    if not cfg.get("bash_compress", True) or cfg.get("shadow"):
        return None, False

    # Support safe `&&`-chained commands when every segment independently passes validation
    chain = _split_safe_and_chain(command)
    if chain:
        wrapped_parts: list[str] = []
        all_readonly = True
        for seg in chain:
            w, ro = _wrap_command_for_compression(seg, cfg, session_id=session_id)
            if not w:
                return None, False
            wrapped_parts.append(w)
            all_readonly = all_readonly and ro
        return " && ".join(wrapped_parts), all_readonly

    try:
        from runtime import shell_safety as _bh
    except Exception:
        return None, False

    if _bh.has_dangerous_chars(command):
        return None, False

    # command-filters.toml: `[exclude]` never wraps; a user-level filter may widen the whitelist
    # and forces the Python compressor (the native exec path does not read user filters).
    user_allowed = False
    has_user_filter = False
    try:
        from runtime import user_filters as _uf

        if _uf.is_excluded(command):
            return None, False
        has_user_filter = _uf.matching_filter(command) is not None
        user_allowed = _uf.user_filter_allows_wrap(command)
    except Exception:
        pass

    compressor = _THIS_DIR / "output_compressor.py"
    if not compressor.is_file():
        return None, False

    fast_bin = _PLUGIN_ROOT / "bin" / ("terser-fast.exe" if os.name == "nt" else "terser-fast")
    safe_sid = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")[:64] if cfg.get("cmd_delta", True) else ""

    def _prefer_native_exec(toks: list[str]) -> bool:
        if not fast_bin.is_file() or not toks or has_user_filter:
            return False
        head = Path(toks[0]).stem.lower()
        sub = toks[1].lower() if len(toks) > 1 else ""
        # Use ~3ms native Rust exec for build/test/lint/pkg/infra/git commands (no Python startup overhead)
        if head in (
            "cargo", "go", "npm", "pnpm", "yarn", "pip", "uv", "tsc",
            "pytest", "jest", "vitest", "docker", "gradle", "gradlew", "dotnet",
            "ruff", "eslint", "mypy", "pyright", "flake8",
        ):
            return True
        if head == "git" and sub in ("diff", "status", "log"):
            return True
        if head in ("python", "python3") and len(toks) >= 3 and sub == "-m":
            return toks[2].lower() in ("unittest", "pytest", "mypy", "ruff")
        return False

    if os.name == "nt":
        tokens = _split_powershell_command(command)
        if not tokens or "=" in tokens[0]:
            return None, False
        if shutil.which(tokens[0]) is None:
            return None, False
        check_str = shlex.join(tokens)
        if _bh.has_dangerous_chars(check_str) or not (user_allowed or _bh.is_whitelisted(check_str)):
            return None, False
        if _prefer_native_exec(tokens):
            sid_arg = f" --sid {_ps_quote(safe_sid)}" if safe_sid else ""
            rewritten = (
                "& "
                + _ps_quote(str(fast_bin))
                + f" exec --host antigravity{sid_arg} -- "
                + " ".join(_ps_quote(t) for t in tokens)
            )
        else:
            from runtime.config import console_python

            py_console = console_python(sys.executable)
            sid_prefix = f"--sid {_ps_quote(safe_sid)} -- " if safe_sid else ""
            rewritten = (
                "& "
                + _ps_quote(py_console)
                + " -E -s "
                + _ps_quote(str(compressor))
                + " "
                + sid_prefix
                + " ".join(_ps_quote(t) for t in tokens)
            )
    else:
        if not (user_allowed or _bh.is_whitelisted(command)):
            return None, False
        try:
            tokens = shlex.split(command)
            if not tokens:
                return None, False
        except ValueError:
            return None, False
        check_str = command
        if _prefer_native_exec(tokens):
            sid_arg = f" --sid {shlex.quote(safe_sid)}" if safe_sid else ""
            rewritten = (
                shlex.quote(str(fast_bin))
                + f" exec --host antigravity{sid_arg} -- "
                + " ".join(shlex.quote(t) for t in tokens)
            )
        else:
            sid_prefix = f"--sid {shlex.quote(safe_sid)} -- " if safe_sid else ""
            rewritten = (
                shlex.quote(sys.executable)
                + " -E -s "
                + shlex.quote(str(compressor))
                + " "
                + sid_prefix
                + " ".join(shlex.quote(t) for t in tokens)
            )

    readonly_ok = False
    if cfg.get("auto_approve_readonly", False):
        try:
            from runtime import shell_safety

            readonly_ok = bool(shell_safety.is_auto_approvable(check_str))
        except Exception:
            readonly_ok = False

    return rewritten, readonly_ok


# ---------------------------------------------------------------------------
# Session-scoped once-marker & live context fill (Antigravity)
# ---------------------------------------------------------------------------

def _session_flag_path(session_id: str, env: dict[str, str]) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:80]
    return state_home(env) / "session-flags" / f"{safe}.json"


def _get_flag(session_id: str, key: str, env: dict[str, str]) -> Any:
    if not session_id:
        return None
    p = _session_flag_path(session_id, env)
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data.get(key) if isinstance(data, dict) else None
    except Exception:
        return None


def _has_flag(session_id: str, key: str, env: dict[str, str]) -> bool:
    return bool(_get_flag(session_id, key, env))


def _set_flag(session_id: str, key: str, env: dict[str, str], value: Any = True) -> None:
    if not session_id:
        return
    p = _session_flag_path(session_id, env)
    data: dict[str, Any] = {}
    try:
        if p.is_file():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data = raw
    except Exception:
        pass
    data[key] = value
    atomic_write_json(p, data)


def _antigravity_live_fill(payload: dict[str, Any], env: dict[str, str]) -> float | None:
    """Live context fill for Antigravity via read-only protobuf DB decoder + transcript fallback."""
    sid = _session_id(payload, "antigravity")
    tp = str(payload.get("transcriptPath") or payload.get("transcript_path") or "")
    try:
        from runtime import antigravity_proto

        fill = antigravity_proto.live_context_fill(sid, tp, env)
        if isinstance(fill, float):
            return fill
    except Exception:
        pass
    if tp:
        try:
            from runtime import autopilot

            return autopilot.context_fill(tp, "antigravity")
        except Exception:
            pass
    return None


def _queue_ag_subagent_check(sid: str, model: str, ws: str, env: dict[str, str]) -> None:
    """Record a pending Antigravity routed subagent so PreInvocation can double-check its reply."""
    if not sid:
        return
    pending = _get_flag(sid, "pending_subagent_checks", env)
    items = list(pending) if isinstance(pending, list) else []
    items.append({"model": model, "cwd": ws, "ts": round(time.time(), 1)})
    _set_flag(sid, "pending_subagent_checks", env, items[-8:])


def _drain_ag_subagent_checks(sid: str, payload: dict[str, Any], ws: str, env: dict[str, str]) -> list[str]:
    """Double-check completed Antigravity routed subagent replies from transcript tail."""
    if not sid:
        return []
    pending = _get_flag(sid, "pending_subagent_checks", env)
    if not isinstance(pending, list) or not pending:
        return []
    tp_raw = str(payload.get("transcriptPath") or payload.get("transcript_path") or "")
    if not tp_raw:
        return []
    tp = Path(tp_raw)
    full_tp = tp.parent / "transcript_full.jsonl"
    target = full_tp if full_tp.is_file() else tp
    if not target.is_file():
        return []
    try:
        with open(target, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 262_144))
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    seen_hashes = set(_get_flag(sid, "verified_subagent_hashes", env) or [])
    import hashlib as _hl
    from runtime import subagent_verify as sv

    replies: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if "sender=" not in line and "SYSTEM_MESSAGE" not in line and "subagent" not in line.lower():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        txt = sv.response_text(rec)
        if not txt or "sender=" not in txt or "content=" not in txt:
            continue
        idx = txt.find("content=")
        body = txt[idx + len("content="):].strip()
        if len(body) < 15:
            continue
        h = _hl.sha256(body[:400].encode("utf-8", errors="ignore")).hexdigest()[:16]
        if h not in seen_hashes:
            replies.append((h, body))

    if not replies:
        return []

    notes: list[str] = []
    remaining = list(pending)
    for h, body in replies:
        if not remaining:
            break
        item = remaining.pop(0)
        seen_hashes.add(h)
        model = str(item.get("model") or "flash")
        cwd = str(item.get("cwd") or ws)
        note = sv.verify_reply_text(body, model, cwd, env)
        if note:
            notes.append(note)
    _set_flag(sid, "pending_subagent_checks", env, remaining)
    _set_flag(sid, "verified_subagent_hashes", env, list(seen_hashes)[-32:])
    return notes


def _ag_system_step(text: str, *, user_markdown: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"systemMessage": text}
    if user_markdown is not None:
        body["metadata"] = {"user_markdown": user_markdown}
    return {"systemMessage": body}


# ---------------------------------------------------------------------------
# Detached Background Rollup & Experience Folding (Stop / SessionEnd)
# ---------------------------------------------------------------------------

def _spawn_detached(cmd: list[str], env: dict[str, str]) -> None:
    from runtime.config import detached_spawn_kwargs, windowless_python

    launch_cmd = list(cmd)
    if sys.platform == "win32" and launch_cmd and launch_cmd[0] == sys.executable:
        launch_cmd[0] = windowless_python(sys.executable)

    base_kwargs: dict[str, Any] = {
        "env": env,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    try:
        subprocess.Popen(launch_cmd, **base_kwargs, **detached_spawn_kwargs(breakaway=True))
    except OSError:
        # Windows job objects may forbid CREATE_BREAKAWAY_FROM_JOB; retry without it
        try:
            subprocess.Popen(launch_cmd, **base_kwargs, **detached_spawn_kwargs(breakaway=False))
        except Exception:
            pass
    except Exception:
        pass


def _touch_lease(name: str, env: dict[str, str]) -> bool:
    lease = state_home(env) / f"{name}.lease"
    try:
        if lease.exists() and (time.time() - lease.stat().st_mtime) < _ROLLUP_LEASE_SECONDS:
            return False
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.touch()
        return True
    except OSError:
        return False


def _trigger_background_fold(host: str, payload: dict[str, Any], env: dict[str, str]) -> None:
    """Spawn ONE lease-debounced background worker to run TO rollup + TE fold."""
    if not _touch_lease(f"rollup-{host}", env):
        return
    # Persist payload to a short-lived temp file so the detached worker has full context
    worker_dir = state_home(env) / "jobs"
    try:
        worker_dir.mkdir(parents=True, exist_ok=True)
        job_file = worker_dir / f"{host}-{os.getpid()}-{int(time.time())}.json"
        job_file.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return

    child_env = dict(env)
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    _spawn_detached(
        [sys.executable, "-E", "-s", str(_THIS_DIR / "unified_hook.py"),
         "--host", host, "--event", "_background_fold", "--job", str(job_file)],
        child_env,
    )


def _run_background_fold(host: str, job_path: str, env: dict[str, str]) -> None:
    """Executed inside the detached background worker (never blocks user turns)."""
    payload: dict[str, Any] = {}
    p = Path(job_path)
    try:
        if p.is_file():
            payload = json.loads(p.read_text(encoding="utf-8"))
            p.unlink(missing_ok=True)
    except Exception:
        pass

    ensure_vendor_paths()

    # 1. Session micro-skills fold (updates EnvFacts/microskills + toolprune ledger).
    # Skipped for mid-session checkpoints: the fold belongs to session end.
    try:
        if payload.get("_terser_trigger") == "pre-fanout":
            raise LookupError("mid-session checkpoint")
        from tokeneconomizer.plugin import hook as te_hook  # type: ignore

        ev = dict(payload)
        ev.setdefault("hook_event_name", "SessionEnd" if host in ("claude", "codex") else "Stop")
        te_hook.run(ev, env, host_hint=host)
    except Exception:
        pass

    # 2. Continuity capture / dashboard refresh (native analytics)
    try:
        from runtime import analytics

        if host == "antigravity":
            analytics.write_dashboard(host, open_browser=False, env=env)
        else:
            trigger = str(payload.get("_terser_trigger") or "stop")
            analytics.compact_capture(payload, trigger, env=env)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Event Handlers (Unified across Antigravity 2.0, Claude Code, Codex)
# ---------------------------------------------------------------------------

def handle_session_start(host: str, payload: dict[str, Any], cfg: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    sid = _session_id(payload, host)
    ws = _workspace(payload, env)
    source = str(payload.get("source") or "startup").lower()

    # Reset read cache on /compact or /clear so post-compaction reads are never suppressed
    if source in ("compact", "clear") or payload.get("is_compact"):
        if source == "compact" or payload.get("is_compact"):
            _set_flag(sid, "recall_pending", env, _RECALL_PROMPTS)
        read_funnel.clear_session_cache(sid, env)
        if host == "codex":
            codex_hooks.remove_refetch_state(sid, env)

    if sid and _has_flag(sid, "brief_sent", env) and source == "startup":
        return {}

    extra_notices: list[str] = []
    if sid and not _get_flag(sid, "started_at", env):
        _set_flag(sid, "started_at", env, round(time.time(), 1))
    if cfg.get("health_check", True) and not cfg.get("shadow"):
        try:
            from runtime import autopilot

            bad = autopilot.health_notice(host, env)
            if bad:
                extra_notices.append(bad)
        except Exception:
            pass
    if cfg.get("index_auto_init", True) and not cfg.get("shadow") and ws:
        try:
            if index_bridge.spawn_auto_index(
                ws, max_files=int(cfg.get("index_auto_init_max_files", 5000)), env=env
            ):
                _set_flag(sid, "auto_index_pending", env, ws)
                extra_notices.append("indexing repo in background")
        except Exception:
            pass

    brief, notice = session_brief.build_session_brief(
        ws, host=host, cfg=cfg, env=env,
        instruction_lines=codex_hooks.instruction_lines(payload.get("transcript_path")) if host == "codex" else (),
    )
    if sid and brief:
        _set_flag(sid, "brief_sent", env)
    if extra_notices:
        notice = " ".join([notice or "Terser active."] + [n.rstrip(".") + "." for n in extra_notices])

    if host == "antigravity":
        steps: list[dict[str, Any]] = []
        if brief:
            steps.append(_ag_system_step(brief))
        if notice:
            # Zero model tokens: renders inline to the user only!
            steps.append(_ag_system_step("", user_markdown=notice))
        return {"injectSteps": steps} if steps else {}

    # Claude Code & Codex contract (ONE JSON object)
    out: dict[str, Any] = {}
    if brief:
        out["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": brief,
        }
    if notice:
        out["systemMessage"] = notice
    return out


_RECALL_PROMPTS = 3


def _post_compact_recall(sid: str, payload: dict[str, Any], cfg: dict[str, Any], env: dict[str, str]) -> str:
    """For the first prompts after a compaction, restore logged facts the prompt refers to."""
    if not sid or cfg.get("shadow") or not cfg.get("compact_recall", True):
        return ""
    left = _get_flag(sid, "recall_pending", env)
    if not isinstance(left, int) or left <= 0:
        return ""
    _set_flag(sid, "recall_pending", env, left - 1)
    try:
        from runtime import context_sandbox

        lines = context_sandbox.recall_events(
            _workspace(payload, env), str(payload.get("prompt") or ""), session_id=sid, env=env
        )
    except Exception:
        return ""
    if not lines:
        return ""
    return "[Terser] Before compaction this session logged:\n" + "\n".join(lines)


def handle_pre_invocation(host: str, payload: dict[str, Any], cfg: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """Antigravity PreInvocation / Claude & Codex UserPromptSubmit."""
    sid = _session_id(payload, host)
    ws = _workspace(payload, env)

    if host == "antigravity":
        steps: list[dict[str, Any]] = []
        try:
            inv_num = int(payload.get("invocationNum") or 0)
        except (TypeError, ValueError):
            inv_num = 0

        # Fallback session brief on first invocation if SessionStart didn't inject
        if inv_num <= 1 and sid and not _has_flag(sid, "brief_sent", env):
            brief, notice = session_brief.build_session_brief(ws, host=host, cfg=cfg, env=env)
            if brief:
                _set_flag(sid, "brief_sent", env)
                steps.append(_ag_system_step(brief))
                if notice:
                    steps.append(_ag_system_step("", user_markdown=notice))

        # Pending unified thrash/loop redirect
        nudge = loop_guard.pop_pending_nudge(sid, cfg=cfg, env=env)
        if nudge:
            steps.append(_ag_system_step(nudge))

        # Double-check any completed low-cost routed subagent replies (zero-token verification)
        if cfg.get("subagent_verify", True) and not cfg.get("shadow"):
            try:
                for vnote in _drain_ag_subagent_checks(sid, payload, ws, env):
                    steps.append(_ag_system_step(vnote))
            except Exception:
                pass

        # Zero-command autopilot: index-ready notice & plain-language intent hints on new user turn
        if not cfg.get("shadow"):
            try:
                from runtime import autopilot as _ap

                pending_ws = _get_flag(sid, "auto_index_pending", env)
                if isinstance(pending_ws, str) and pending_ws:
                    idx = index_bridge.workspace_index_summary(pending_ws, env=env)
                    if idx:
                        _set_flag(sid, "auto_index_pending", env, False)
                        steps.append(_ag_system_step(
                            f"[Terser] Code index `{idx['repo']}` is now ready; use `order`/`route` for symbol lookups."
                        ))
                if inv_num <= 1 and cfg.get("intent_hints", True):
                    user_prompt = str(payload.get("lastUserInput") or "")
                    hint = _ap.intent_hint(user_prompt, host)
                    if hint:
                        steps.append(_ag_system_step(hint))
            except Exception:
                pass

        # Live context-fill nudges (fast-path skipped on early turns)
        if cfg.get("context_fill_nudges", True) and inv_num >= 4:
            fill = _antigravity_live_fill(payload, env)
            if isinstance(fill, (int, float)) and 0.0 <= fill <= 1.0:
                if fill >= 0.85 and not _has_flag(sid, "fill_85", env):
                    _set_flag(sid, "fill_85", env)
                    steps.append(_ag_system_step(
                        "[Terser] Context >=85% full. Keep edits focused and replies concise; "
                        "use /compact or start fresh soon."
                    ))
                elif fill >= 0.70 and not _has_flag(sid, "fill_70", env):
                    _set_flag(sid, "fill_70", env)
                    steps.append(_ag_system_step(
                        "[Terser] Context >=70% full. Prefer symbol lookups, scoped CLI output, "
                        "and concise replies without repeating unchanged code."
                    ))
        return {"injectSteps": steps} if steps else {}

    # Claude Code / Codex UserPromptSubmit
    parts: list[str] = []
    nudge = loop_guard.pop_pending_nudge(sid, cfg=cfg, env=env)
    if nudge:
        parts.append(nudge)
    recall = _post_compact_recall(sid, payload, cfg, env)
    if recall:
        parts.append(recall)
    if not cfg.get("shadow"):
        try:
            parts.extend(_autopilot_prompt_context(host, sid, payload, cfg, env))
        except Exception:
            pass
    if parts:
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "\n\n".join(parts),
            }
        }
    return {}


def _autopilot_prompt_context(
    host: str, sid: str, payload: dict[str, Any], cfg: dict[str, Any], env: dict[str, str]
) -> list[str]:
    """Zero-command context: index-ready notice, plain-language intents, live context fill."""
    from runtime import autopilot

    out: list[str] = []
    pending_ws = _get_flag(sid, "auto_index_pending", env)
    if isinstance(pending_ws, str) and pending_ws:
        idx = index_bridge.workspace_index_summary(pending_ws, env=env)
        if idx:
            _set_flag(sid, "auto_index_pending", env, False)
            out.append(f"[Terser] Code index `{idx['repo']}` is now ready; use `order`/`route` for symbol lookups.")
    if cfg.get("intent_hints", True):
        hint = autopilot.intent_hint(str(payload.get("prompt") or ""), host)
        if hint:
            out.append(hint)
    if cfg.get("context_fill_nudges", True) and sid:
        fill = autopilot.context_fill(payload.get("transcript_path"), host)
        if fill is not None:
            if fill >= 0.85 and not _has_flag(sid, "fill_85", env):
                _set_flag(sid, "fill_85", env)
                _set_flag(sid, "fill_70", env)
                out.append(autopilot.FILL_85)
            elif fill >= 0.70 and not _has_flag(sid, "fill_70", env):
                _set_flag(sid, "fill_70", env)
                out.append(autopilot.FILL_70)
    return out


def _contextignore_reason(fp: str, ws: str, cfg: dict[str, Any], env: dict[str, str]) -> str | None:
    """Deny reason when ``fp`` matches a ``.contextignore`` pattern (never in shadow mode)."""
    if not fp or cfg.get("shadow") or not cfg.get("contextignore", True):
        return None
    try:
        from runtime import user_filters

        pattern = user_filters.ignored_by(fp, ws, env)
        if pattern:
            return user_filters.deny_reason(fp, pattern)
    except Exception:
        return None
    return None


def handle_pre_tool_use(host: str, payload: dict[str, Any], cfg: dict[str, Any], env: dict[str, str]) -> dict[str, Any] | None:
    """Returns dict to emit, or None for silent passthrough (critical for Antigravity!)."""
    sid = _session_id(payload, host)
    ws = _workspace(payload, env)

    if host == "antigravity":
        tc = payload.get("toolCall")
        if not isinstance(tc, dict):
            return None
        tool_name = str(tc.get("name") or "")
        args = tc.get("args") if isinstance(tc.get("args"), dict) else {}

        # 1. File read -> 4-Tier Read Funnel
        if tool_name in ("read_file", "view_file"):
            fp = str(args.get("Path") or args.get("AbsolutePath") or args.get("path") or "")
            blocked = _contextignore_reason(fp, ws, cfg, env)
            if blocked:
                return {"decision": "deny", "reason": blocked}
            res = read_funnel.check_read(
                sid, fp,
                start_line=read_funnel._parse_int(args.get("StartLine")),
                end_line=read_funnel._parse_int(args.get("EndLine")),
                byte_offset=read_funnel._parse_int(args.get("ByteOffset")),
                cfg=cfg, host=host, env=env,
            )
            if res and res.get("action") == "deny":
                return {"decision": "deny", "reason": res["reason"]}
            return None

        # 1b. Large grep_search -> answer directly from fresh AST index once
        if tool_name == "grep_search" and cfg.get("index_grep_answer", True) and not cfg.get("shadow"):
            grep_inp = {
                "pattern": args.get("Query") or args.get("query") or "",
                "path": args.get("SearchPath") or args.get("path") or ws,
            }
            answer = index_bridge.try_answer_large_grep(grep_inp, ws, sid)
            if answer:
                return {"decision": "deny", "reason": answer}
            return None

        # 2. Read-only MCP refetch guard
        if tool_name == "call_mcp_tool" or tool_name.startswith("mcp_"):
            if tool_name == "call_mcp_tool":
                srv = str(args.get("ServerName") or "")
                inner = str(args.get("ToolName") or "")
                disp = f"{srv}:{inner}" if srv else inner
                inner_args = args.get("Arguments")
            else:
                disp = tool_name
                inner_args = args
            res = read_funnel.check_mcp_refetch(sid, disp, inner_args, cfg=cfg, host=host, env=env)
            if res and res.get("action") == "deny":
                return {"decision": "deny", "reason": res["reason"]}
            return None

        # 3. Shell command -> FormRepair FIRST, then Output Compression + Command Delta SECOND!
        if tool_name == "run_command":
            cmd = args.get("CommandLine")
            if not isinstance(cmd, str) or not cmd.strip():
                return None
            repaired_cmd, was_repaired = loop_guard.apply_form_repair(ws, cmd, cfg=cfg, host=host, env=env)
            wrapped, readonly_ok = _wrap_command_for_compression(repaired_cmd, cfg, session_id=sid)
            if wrapped:
                decision = "allow" if readonly_ok else "ask"
                return {"decision": decision, "overwrite": {"CommandLine": wrapped}}
            if was_repaired:
                return {"decision": "ask", "overwrite": {"CommandLine": repaired_cmd}}
            return None

        # 4. Subagent / Swarm spawn -> inject compact Terser Subagent economy brief (Antigravity 2.0)
        if not cfg.get("shadow"):
            if tool_name == "invoke_subagent":
                type_name = str(args.get("TypeName") or "").strip().lower()
                prompt = args.get("Prompt")
                if (
                    type_name not in _SKIP_AG_SUBAGENT_TYPES
                    and isinstance(prompt, str)
                    and prompt.strip()
                    and "[Terser Subagent]" not in prompt
                ):
                    overwrite: dict[str, Any] = {
                        "Prompt": f"{prompt.rstrip()}\n\n{_SUBAGENT_BRIEF_AG}"
                    }
                    # Zero-cost complexity routing: downgrade clearly routine lookup subagents to the
                    # user's chosen Antigravity low-cost tier only when Model was omitted or `inherit`.
                    if cfg.get("subagent_model_routing", False):
                        try:
                            from runtime import subagent_verify as sv

                            ok, _why = sv.should_route(prompt, type_name, str(args.get("Model") or ""), env)
                            if ok:
                                model = str(
                                    cfg.get("subagent_cheap_model_antigravity") or sv.CHEAP_MODELS["antigravity"]
                                ).strip()
                                if model not in sv.AGY_SUBAGENT_MODELS:
                                    model = sv.CHEAP_MODELS["antigravity"]
                                effort = str(cfg.get("subagent_cheap_effort_antigravity") or "").strip().lower()
                                overwrite["Model"] = model
                                if cfg.get("subagent_verify", True):
                                    overwrite["Prompt"] = sv.routed_prompt(overwrite["Prompt"], effort=effort)
                                    _queue_ag_subagent_check(sid, model, ws, env)
                                sv.record_routed(type_name, model, env)
                        except Exception:
                            pass
                    return {"decision": "allow", "overwrite": overwrite}
            elif tool_name == "swarm":
                action = str(args.get("action") or "").strip().lower()
                brief = args.get("brief")
                if (
                    action == "add"
                    and isinstance(brief, str)
                    and brief.strip()
                    and "[Terser Subagent]" not in brief
                ):
                    sw_overwrite: dict[str, Any] = {"brief": f"{brief.rstrip()}\n\n{_SUBAGENT_BRIEF_AG}"}
                    if cfg.get("subagent_model_routing", False) and not str(args.get("model") or "").strip():
                        try:
                            from runtime import subagent_verify as sv

                            ok, _why = sv.should_route(brief, "research", "", env)
                            if ok:
                                model = str(
                                    cfg.get("subagent_cheap_model_antigravity") or sv.CHEAP_MODELS["antigravity"]
                                ).strip()
                                effort = str(cfg.get("subagent_cheap_effort_antigravity") or "").strip().lower()
                                sw_overwrite["model"] = model
                                if cfg.get("subagent_verify", True):
                                    sw_overwrite["brief"] = sv.routed_prompt(sw_overwrite["brief"], effort=effort)
                                sv.record_routed("swarm", model, env)
                        except Exception:
                            pass
                    return {"decision": "allow", "overwrite": sw_overwrite}
            elif tool_name == "define_subagent":
                sys_prompt = args.get("SystemPrompt")
                if (
                    isinstance(sys_prompt, str)
                    and sys_prompt.strip()
                    and "[Terser Subagent]" not in sys_prompt
                ):
                    return {"decision": "allow", "overwrite": {"SystemPrompt": f"{sys_prompt.rstrip()}\n\n{_SUBAGENT_BRIEF_AG}"}}

        return None

    # --- Claude Code & Codex PreToolUse ---
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}

    # 1. Read -> 4-Tier Read Funnel
    if tool_name == "Read":
        fp = str(tool_input.get("file_path") or tool_input.get("path") or "")
        blocked = _contextignore_reason(fp, ws, cfg, env)
        if blocked:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": blocked,
                }
            }
        if host == "codex":
            return None  # A proposed read alone is never evidence of delivery.
        offset = read_funnel._parse_int(tool_input.get("offset"))
        limit = read_funnel._parse_int(tool_input.get("limit"))
        end_line = (offset + limit - 1) if (offset is not None and limit is not None) else limit
        res = read_funnel.check_read(
            sid, fp, start_line=offset, end_line=end_line,
            cfg=cfg, host=host, env=env,
        )
        if res and res.get("action") == "deny":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": res["reason"],
                }
            }
        return None

    # 2. Large Grep -> answer directly from fresh AST index once
    if host != "codex" and tool_name == "Grep" and cfg.get("index_grep_answer", True) and not cfg.get("shadow"):
        answer = index_bridge.try_answer_large_grep(tool_input, ws, sid)
        if answer:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": answer,
                }
            }
        return None

    # 3. Bash/Shell/PowerShell -> FormRepair via updatedInput (Claude & Codex)
    if tool_name in _SHELL_TOOL_NAMES:
        if host == "codex" and (tool_name != "Bash" or not cfg.get("codex_form_repair")):
            return None
        is_ps = tool_name in _POWERSHELL_TOOL_NAMES
        cmd_key = "command" if "command" in tool_input else ("cmd" if "cmd" in tool_input else "command")
        cmd = tool_input.get(cmd_key)
        if isinstance(cmd, str) and cmd.strip():
            repaired, was_repaired = loop_guard.apply_form_repair(
                ws, cmd, cfg=cfg, host=host, env=env, shell_neutral_only=is_ps,
            )
            if was_repaired:
                if host == "codex":
                    return codex_hooks.allow_command(tool_input, repaired)
                updated = dict(tool_input, **{cmd_key: repaired})
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "updatedInput": updated,
                    }
                }
        return None

    # 4. Read-only MCP refetch guard
    if tool_name.startswith("mcp__"):
        if host == "codex":
            return codex_hooks.mcp_refetch(sid, payload, cfg, env, completed=False)
        res = read_funnel.check_mcp_refetch(sid, tool_name, tool_input, cfg=cfg, host=host, env=env)
        if res and res.get("action") == "deny":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": res["reason"],
                }
            }
        return None

    # 5a. Checkpoint continuity before subagent fan-out (detached, lease-debounced)
    if host == "claude" and tool_name in ("Agent", "Task") and cfg.get("fanout_checkpoint", True) and not cfg.get("shadow"):
        _trigger_background_fold(host, dict(payload, _terser_trigger="pre-fanout"), env)

    # 5. Claude Code Agent/Task subagent complexity routing (only when model is unset on Explore/read-only lookups)
    if (
        host == "claude"
        and tool_name in ("Agent", "Task")
        and cfg.get("subagent_model_routing", False)
        and not cfg.get("shadow")
    ):
        sub_type = str(tool_input.get("subagent_type") or "").strip().lower()
        prompt = str(tool_input.get("prompt") or "")
        try:
            from runtime import subagent_verify as sv

            ok, _why = sv.should_route(prompt, sub_type, str(tool_input.get("model") or ""), env)
            if ok:
                model = str(cfg.get("subagent_cheap_model") or sv.CHEAP_MODELS["claude"])
                updated = dict(tool_input, model=model)
                effort = str(cfg.get("subagent_cheap_effort") or "").strip().lower()
                if effort in sv.EFFORT_LEVELS and not tool_input.get("effort"):
                    updated["effort"] = effort
                if cfg.get("subagent_verify", True):
                    updated["prompt"] = sv.routed_prompt(prompt)
                sv.record_routed(sub_type, model, env)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "updatedInput": updated,
                    }
                }
        except Exception:
            pass
        return None

    return None


def _mcp_text(raw_text: str) -> str:
    """Plain text of an MCP result that may arrive JSON-encoded (content items, nested strings)."""
    stripped = raw_text.lstrip()
    if not stripped.startswith(("{", "[", '"')):
        return raw_text
    try:
        data = json.loads(stripped)
    except ValueError:
        return raw_text
    parts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return "\n".join(parts) or raw_text


def _refresh_code_mode_edits(sid: str, ws: str, raw_text: str, env: dict[str, str]) -> None:
    try:
        from runtime import code_mode, context_sandbox

        for fp in code_mode.edited_paths_from_output(_mcp_text(raw_text))[:200]:
            read_funnel.invalidate_on_edit(sid, fp, env=env)
            context_sandbox.record_session_event(sid, ws, "edit", f"{Path(fp).name} (code mode)", env=env)
    except Exception:
        pass


def _seed_reads_from_index_output(sid: str, ws: str, raw_text: str, env: dict[str, str]) -> None:
    """Seed read-cache intervals when terser-index returns symbol source spans (`(path Lstart-Lend)`).

    Also drops cached reads for files a Code Mode call wrote (`[terser-code-mode] edited:`),
    so the next Read of them returns fresh content instead of "unchanged".
    """
    if not sid or not ws or not raw_text:
        return
    import re as _re

    if "[terser-code-mode] edited:" in raw_text:
        _refresh_code_mode_edits(sid, ws, raw_text, env)
    root = Path(ws)
    seeded = 0
    for m in _re.finditer(r"\(([^()\r\n]+?)\s+L(\d+)-L?(\d+)\)", raw_text):
        rel = m.group(1).strip()
        try:
            s_ln = int(m.group(2))
            e_ln = int(m.group(3))
        except ValueError:
            continue
        p = Path(rel)
        target = p if p.is_absolute() else (root / rel)
        if target.is_file():
            read_funnel.seed_read_interval(sid, str(target), s_ln, e_ln, env=env)
            seeded += 1
            if seeded >= 12:
                break


def handle_post_tool_use(
    host: str,
    payload: dict[str, Any],
    cfg: dict[str, Any],
    env: dict[str, str],
    *,
    is_failure_event: bool = False,
) -> dict[str, Any]:
    sid = _session_id(payload, host)

    if host == "antigravity":
        tc = payload.get("toolCall") if isinstance(payload.get("toolCall"), dict) else {}
        tool_name = str(tc.get("name") or "")
        args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
        is_err = bool(str(payload.get("error") or "").strip())

        # Edit/Write -> seed read-cache after successful write/edit (or invalidate on error) + AST reindex + continuity
        ws = _workspace(payload, env)
        if tool_name in ("edit_file", "write_file", "replace_file_content", "multi_replace_file_content"):
            fp = str(args.get("Path") or args.get("TargetFile") or args.get("AbsolutePath") or args.get("path") or "")
            if fp:
                if tool_name == "write_file" and not is_err and not args.get("Append"):
                    written_ag = next(
                        (args[k] for k in ("CodeContent", "Content", "content") if isinstance(args.get(k), str)),
                        None,
                    )
                    read_funnel.seed_after_write(sid, fp, written=written_ag, env=env)
                elif tool_name in ("edit_file", "replace_file_content") and not is_err:
                    old_ag = next(
                        (args[k] for k in ("Target", "TargetContent", "OldText", "old_string") if isinstance(args.get(k), str)),
                        None,
                    )
                    new_ag = next(
                        (args[k] for k in ("Replacement", "ReplacementContent", "NewText", "new_string") if isinstance(args.get(k), str)),
                        None,
                    )
                    read_funnel.seed_after_edit(
                        sid,
                        fp,
                        old_text=old_ag,
                        new_text=new_ag,
                        env=env,
                    )
                else:
                    read_funnel.invalidate_on_edit(sid, fp, env=env)
                if cfg.get("index_auto_reindex", True):
                    index_bridge.spawn_reindex_file(fp, env=env)
                if ws and not cfg.get("shadow"):
                    from runtime import context_sandbox

                    context_sandbox.record_session_event(sid, ws, "edit", Path(fp).name, env=env)

        if (tool_name == "call_mcp_tool" or tool_name.startswith("mcp_")) and not is_err and not cfg.get("shadow"):
            srv = str(args.get("ServerName") or "")
            if tool_name == "call_mcp_tool":
                inner = str(args.get("ToolName") or "")
                disp = f"{srv}:{inner}" if srv else inner
                inner_args = args.get("Arguments")
            else:
                disp = tool_name
                inner_args = args
            resp_val = payload.get("response") or payload.get("result") or payload.get("toolResponse")
            resp_txt = resp_val if isinstance(resp_val, str) else json.dumps(resp_val, default=str) if resp_val is not None else ""
            if resp_txt:
                read_funnel.record_mcp_response_size(sid, disp, inner_args, len(resp_txt.encode("utf-8", errors="replace")), env=env)
                srv_low = srv.lower()
                if "terser-index" in srv_low or "terser_index" in srv_low or "terser" in tool_name.lower():
                    _seed_reads_from_index_output(sid, ws, resp_txt, env)

        if is_err and ws and not cfg.get("shadow"):
            from runtime import context_sandbox

            err_snip = str(payload.get("error") or "")[:120]
            context_sandbox.record_session_event(sid, ws, "error", f"{tool_name}: {err_snip}", env=env)

        loop_guard.record_tool_event(sid, tool_name, args, is_error=is_err, cfg=cfg, env=env)
        return {}

    # --- Claude Code & Codex PostToolUse / PostToolUseFailure ---
    tool_name = str(payload.get("tool_name") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    tool_resp = payload.get("tool_response")
    is_err = is_failure_event or (codex_hooks.tool_failed(payload) if host == "codex" else bool(payload.get("error")))

    # 1. Edit/Write/apply_patch -> read-cache soft-deny reset + incremental AST auto-reindex
    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"):
        edited_paths: list[str] = []
        direct = str(tool_input.get("file_path") or tool_input.get("path") or "").strip()
        if direct:
            edited_paths.append(direct)
        if tool_name == "apply_patch":
            import re as _re
            patch_text = next(
                (v for v in (tool_input.get("input"), tool_input.get("patch"), tool_input.get("command"))
                 if isinstance(v, str)),
                "",
            )
            for m in _re.finditer(r"^\*\*\* (?:(?:Update|Add|Delete) File|Move to):[ \t]*(.+?)[ \t\r]*$", patch_text, _re.M):
                p = m.group(1).strip()
                if p and p not in edited_paths:
                    edited_paths.append(p)
        ws_cl = _workspace(payload, env)
        for fp in edited_paths:
            if host == "codex" and not Path(fp).is_absolute():
                fp = str(Path(ws_cl) / fp)
            if tool_name == "Write" and not is_err:
                read_funnel.seed_after_write(
                    sid, fp,
                    written=tool_input.get("content") if isinstance(tool_input.get("content"), str) else None,
                    env=env,
                )
            elif tool_name == "Edit" and not is_err:
                read_funnel.seed_after_edit(
                    sid,
                    fp,
                    old_text=tool_input.get("old_string") if isinstance(tool_input.get("old_string"), str) else None,
                    new_text=tool_input.get("new_string") if isinstance(tool_input.get("new_string"), str) else None,
                    replace_all=bool(tool_input.get("replace_all")),
                    env=env,
                )
            elif tool_name == "MultiEdit" and not is_err:
                raw_edits = tool_input.get("edits") if isinstance(tool_input.get("edits"), list) else []
                multi: list[tuple[str, str, bool]] = [
                    (e["old_string"], e["new_string"], bool(e.get("replace_all")))
                    for e in raw_edits
                    if isinstance(e, dict) and isinstance(e.get("old_string"), str) and isinstance(e.get("new_string"), str)
                ]
                read_funnel.seed_after_edit(
                    sid, fp, edits=multi if len(multi) == len(raw_edits) else [], env=env,
                )
            else:
                read_funnel.invalidate_on_edit(sid, fp, env=env)
            if cfg.get("index_auto_reindex", True):
                index_bridge.spawn_reindex_file(fp, env=env)
            if ws_cl and not cfg.get("shadow"):
                from runtime import context_sandbox

                context_sandbox.record_session_event(sid, ws_cl, "edit", Path(fp).name, env=env)

    hso: dict[str, Any] = {}
    extra_context: list[str] = []

    if is_err and not cfg.get("shadow") and cfg.get("health_check", True):
        from runtime import autopilot

        mcp_hint = autopilot.mcp_failure_hint(tool_name, host)
        if mcp_hint:
            extra_context.append(mcp_hint)

    # 1b. Edit-miss recovery: show the exact closest region instead of forcing a full re-read
    if (
        host == "claude"
        and is_err
        and tool_name in ("Edit", "MultiEdit")
        and cfg.get("edit_miss_hints", True)
        and not cfg.get("shadow")
    ):
        try:
            from runtime import edit_assist

            err_text = payload.get("error")
            if not isinstance(err_text, str):
                err_text = json.dumps(tool_resp, default=str) if tool_resp is not None else ""
            found = edit_assist.hint_for_tool_failure(tool_name, tool_input, err_text)
            if found:
                hint_fp, hint, shown = found
                extra_context.append(hint)
                if shown:
                    read_funnel.seed_read_interval(sid, hint_fp, shown[0], shown[1], env=env)
                record_funnel_event("edit_hint", "served", host=host, env=env)
        except Exception:
            pass

    # 2. Invisible PostToolUse Bash/Shell/PowerShell output compression (stdout + stderr) + Command Delta (Claude Code)
    if (
        host == "claude"
        and tool_name in _SHELL_TOOL_NAMES
        and cfg.get("bash_compress", True)
        and not cfg.get("shadow")
        and isinstance(tool_resp, dict)
    ):
        cmd = str(tool_input.get("command") or tool_input.get("cmd") or "")
        ws_post = _workspace(payload, env)
        enhanced = bool(cfg.get("enhanced_compression", True))
        updated_resp: dict[str, Any] | None = None

        stdout = tool_resp.get("stdout")
        if isinstance(stdout, str) and len(stdout) > 300:
            comp, stats = output_compressor.compress_output(
                stdout, command=cmd, enhanced=enhanced,
                host=host, workspace=ws_post, env=env,
            )
            if cfg.get("cmd_delta", True) and not stats.get("excluded"):
                comp, delta_used = cmd_delta.apply_command_delta(
                    sid, cmd, comp, returncode=1 if is_err else 0, host=host, env=env,
                )
                if delta_used:
                    stats["served_chars"] = len(comp)
            if stats["served_chars"] < int(stats["raw_chars"] * 0.85):
                updated_resp = dict(tool_resp, stdout=comp)

        stderr = tool_resp.get("stderr")
        if isinstance(stderr, str) and len(stderr) > 300:
            comp_err, err_stats = output_compressor.compress_output(
                stderr, command=cmd, returncode=1 if is_err else 0, enhanced=enhanced,
                host=host, workspace=ws_post, env=env,
            )
            if err_stats["served_chars"] < int(err_stats["raw_chars"] * 0.85):
                updated_resp = dict(updated_resp if updated_resp is not None else tool_resp, stderr=comp_err)

        if updated_resp is not None:
            hso["hookEventName"] = "PostToolUse"
            hso["updatedToolOutput"] = updated_resp

    # Codex consumes the original result. Bookkeeping has no output rewrite.
    if host == "codex" and tool_name.startswith("mcp__"):
        if is_err and sid:
            codex_hooks.remove_refetch_state(sid, env)
        elif not is_err:
            codex_hooks.mcp_refetch(sid, payload, cfg, env, completed=True)

    # 3. Oversized MCP output compression + archival + terser-index read-cache seeding + real size telemetry (Claude Code)
    if (
        host == "claude"
        and tool_name.startswith("mcp__")
        and not cfg.get("shadow")
    ):
        raw_text = tool_resp if isinstance(tool_resp, str) else json.dumps(tool_resp, default=str) if tool_resp is not None else ""
        if raw_text and not is_err:
            read_funnel.record_mcp_response_size(sid, tool_name, tool_input, len(raw_text.encode("utf-8", errors="replace")), env=env)
        if "terser-index" in tool_name.lower() or "terser_index" in tool_name.lower():
            _seed_reads_from_index_output(sid, _workspace(payload, env), raw_text, env)
        threshold = int(cfg.get("archive_threshold_bytes", 16384))
        ws_mcp = _workspace(payload, env)
        if cfg.get("archive_large_outputs", True) and len(raw_text) > threshold:
            comp, stats = output_compressor.compress_output(
                raw_text, command=tool_name, max_chars=threshold // 2,
                enhanced=bool(cfg.get("enhanced_compression", True)),
                host=host, workspace=ws_mcp, env=env,
            )
            if stats["served_chars"] < int(stats["raw_chars"] * 0.85):
                hso["hookEventName"] = "PostToolUse"
                hso["updatedMCPToolOutput"] = comp
        elif cfg.get("enhanced_compression", True) and len(raw_text) > 1500:
            # Strictly lossless compaction for medium MCP payloads (pretty JSON, uniform arrays, tables, HTML/MD)
            comp, stats = output_compressor.compress_output(
                raw_text, command=tool_name,
                enhanced=True, lossless_only=True,
                host=host, workspace=ws_mcp, env=env,
            )
            if stats["served_chars"] < int(stats["raw_chars"] * 0.85):
                hso["hookEventName"] = "PostToolUse"
                hso["updatedMCPToolOutput"] = comp

    # 3a. WebFetch lossless HTML/Markdown boilerplate compaction (Claude Code)
    if (
        host == "claude"
        and tool_name == "WebFetch"
        and cfg.get("enhanced_compression", True)
        and not cfg.get("shadow")
    ):
        raw_web = tool_resp if isinstance(tool_resp, str) else ""
        web_key = ""
        if not raw_web and isinstance(tool_resp, dict):
            web_key = next((k for k in ("result", "content", "text", "output") if isinstance(tool_resp.get(k), str)), "")
            raw_web = tool_resp[web_key] if web_key else ""
        if len(raw_web) > 800:
            comp_web = output_compressor.compact_web_and_markdown(raw_web)
            comp_web = output_compressor.compact_aligned_tables(comp_web)
            served_web = comp_web
            if cfg.get("web_archive", True) and not is_err:
                archived = output_compressor.archive_web_page(
                    comp_web, source=str(tool_input.get("url") or ""),
                    threshold=int(cfg.get("web_archive_threshold", 12000)), env=env,
                )
                if archived:
                    served_web = archived
            if len(served_web) < int(len(raw_web) * 0.85):
                if served_web is comp_web:
                    record_savings("web_compact", len(raw_web), len(comp_web), host=host, detail="WebFetch", env=env)
                hso["hookEventName"] = "PostToolUse"
                hso["updatedToolOutput"] = dict(tool_resp, **{web_key: served_web}) if web_key else served_web

    # 3b. Runaway Glob / Grep output guard (Claude Code & Codex): group by file/dir + cap huge scans + FTS5 archive
    if (
        host == "claude"
        and tool_name in ("Glob", "Grep")
        and cfg.get("archive_large_outputs", True)
        and not cfg.get("shadow")
    ):
        raw_text = tool_resp if isinstance(tool_resp, str) else json.dumps(tool_resp, default=str) if tool_resp is not None else ""
        basis = None
        if isinstance(tool_resp, dict):
            basis = json.dumps(
                {k: v for k, v in tool_resp.items() if not _VOLATILE_RESULT_KEY_RE.search(str(k))},
                sort_keys=True, default=str,
            )
        repeat = (
            read_funnel.check_search_repeat(
                sid, tool_name, tool_input, raw_text, basis=basis, cwd=_workspace(payload, env), host=host, env=env,
            )
            if cfg.get("search_repeat_elision", True) and not is_err
            else None
        )
        if repeat is not None:
            hso["hookEventName"] = "PostToolUse"
            hso["updatedToolOutput"] = repeat
            raw_text = ""
        if not raw_text:
            grouped = ""
        elif tool_name == "Grep":
            grouped = output_compressor.group_grep_output(raw_text)
        else:
            grouped = output_compressor.compact_file_list(raw_text.splitlines())
        lines = grouped.splitlines()
        if len(raw_text) > 8192 and len(lines) > 120:
            aid = output_compressor._archive_full_output(raw_text, command=tool_name, env=env)
            from runtime import context_sandbox

            ptr = f" ({context_sandbox.retrieval_hint(aid, env=env)})" if aid else ""
            capped = "\n".join(lines[:60]) + f"\n... [{len(lines) - 60} more {tool_name} lines omitted{ptr}]"
            hso["hookEventName"] = "PostToolUse"
            hso["updatedToolOutput"] = capped
        elif raw_text and len(grouped) < int(len(raw_text) * 0.85):
            hso["hookEventName"] = "PostToolUse"
            hso["updatedToolOutput"] = grouped

    # 3c. Double check of a low-cost routed subagent (zero tokens unless it finds a problem)
    if (
        host == "claude"
        and tool_name in ("Agent", "Task")
        and not is_err
        and not cfg.get("shadow")
        and cfg.get("subagent_verify", True)
    ):
        try:
            from runtime import subagent_verify

            note = subagent_verify.check_routed_result(tool_input, tool_resp, _workspace(payload, env), env)
            if note:
                extra_context.append(note)
        except Exception:
            pass

    # 4. Unified Loop/Thrash Guard + live error continuity event
    if is_err and not cfg.get("shadow"):
        ws_err = _workspace(payload, env)
        if ws_err:
            from runtime import context_sandbox

            err_snip = str(payload.get("error") or "")[:120]
            context_sandbox.record_session_event(sid, ws_err, "error", f"{tool_name}: {err_snip}", env=env)
    nudge = loop_guard.record_tool_event(sid, tool_name, tool_input, is_error=is_err, cfg=cfg, env=env)
    if nudge:
        extra_context.append(nudge)
    if extra_context:
        hso["hookEventName"] = "PostToolUseFailure" if is_failure_event and host != "codex" else "PostToolUse"
        hso["additionalContext"] = "\n\n".join(extra_context)

    return {"hookSpecificOutput": hso} if hso else {}


def handle_pre_compact(host: str, payload: dict[str, Any], cfg: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    sid = _session_id(payload, host)
    if str(payload.get("hook_event_name") or "").lower() in ("precompact", "postcompact", ""):
        _set_flag(sid, "recall_pending", env, _RECALL_PROMPTS)
    read_funnel.clear_session_cache(sid, env)
    if host == "codex":
        codex_hooks.remove_refetch_state(sid, env)
    _trigger_background_fold(host, dict(payload, _terser_trigger="auto"), env)
    # On Claude Code PreCompact, name what the summary must keep so active files,
    # decisions, and errors survive compaction without re-reads.
    if host == "claude" and not cfg.get("shadow"):
        try:
            from runtime import analytics

            text = analytics.compact_instructions(payload)
            if text:
                return {"hookSpecificOutput": {"hookEventName": "PreCompact", "additionalContext": text}}
        except Exception:
            pass
    return {}


def handle_subagent_start(host: str, payload: dict[str, Any], cfg: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """Inject a compact (~45-token) economy brief into spawned subagents (Claude & Codex)."""
    if cfg.get("shadow"):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "additionalContext": _SUBAGENT_BRIEF_CC,
        }
    }


def handle_subagent_stop(host: str, payload: dict[str, Any], cfg: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """Clean up ephemeral per-subagent state files without touching the parent session."""
    sid = _session_id(payload, host)
    if sid and "--" in sid:
        read_funnel.clear_session_cache(sid, env)
        loop_guard.clear_session_state(sid, env)
        cmd_delta.clear_session_state(sid, env)
        if host == "codex":
            codex_hooks.remove_refetch_state(sid, env)
    return {}


def handle_stop(host: str, payload: dict[str, Any], cfg: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    if cfg.get("shadow"):
        return {}
    _trigger_background_fold(host, payload, env)
    if host not in ("claude", "codex") or not cfg.get("session_summary", True):
        return {}
    ev = str(payload.get("hook_event_name") or "").lower()
    if ev in ("sessionend", "stopfailure") or payload.get("stop_hook_active"):
        return {}
    try:
        from runtime import autopilot

        sid = _session_id(payload, host)
        started = _get_flag(sid, "started_at", env)
        if not sid or not isinstance(started, (int, float)):
            return {}
        if host == "claude":
            from runtime import hud

            if hud.is_active(env):  # the desktop status band already shows live savings
                return {}
        saved = autopilot.session_saved_tokens(float(started), env)
        last = int(_get_flag(sid, "summary_shown", env) or 0)
        if autopilot.next_milestone(saved, last):
            _set_flag(sid, "summary_shown", env, saved)
            return {"systemMessage": autopilot.summary_line(saved)}
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def dispatch(
    event: str,
    payload: dict[str, Any],
    *,
    host_hint: str | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Pure dispatch function (directly unit-testable with zero stdin/stdout I/O)."""
    env = env if env is not None else os.environ
    if host_hint in ("opencode", "cursor"):
        # Foreign hosts: translate to the Claude-shaped contract, reuse the handlers below.
        from runtime import host_adapters

        return host_adapters.dispatch_foreign(host_hint, event, payload, env, dispatch)
    cfg = load_config(env)
    host = _detect_host(payload, env, host_hint)
    from runtime import context_sandbox

    context_sandbox.set_hint_host(host)

    if not cfg.get("enabled", True):
        return None if (host == "antigravity" and event == "PreToolUse") else {}

    ev = (event or payload.get("hook_event_name") or payload.get("hookEventName") or "").strip()
    low = ev.lower().replace("-", "").replace("_", "")

    if low == "sessionstart":
        return handle_session_start(host, payload, cfg, env)
    if low in ("preinvocation", "userpromptsubmit"):
        return handle_pre_invocation(host, payload, cfg, env)
    if low == "pretooluse":
        return handle_pre_tool_use(host, payload, cfg, env)
    if low == "posttooluse":
        return handle_post_tool_use(host, payload, cfg, env, is_failure_event=False)
    if low == "posttoolusefailure":
        return handle_post_tool_use(host, payload, cfg, env, is_failure_event=True)
    if host == "codex" and low == "postcompact":
        sid = _session_id(payload, host)
        codex_hooks.remove_refetch_state(sid, env)
        read_funnel.clear_session_cache(sid, env)
        return {}
    if low in ("precompact", "postcompact", "cwdchanged"):
        return handle_pre_compact(host, payload, cfg, env)
    if low == "subagentstart":
        return handle_subagent_start(host, payload, cfg, env)
    if low == "subagentstop":
        return handle_subagent_stop(host, payload, cfg, env)
    if low in ("stop", "sessionend", "stopfailure"):
        if host == "codex" and low == "sessionend" and (sid := _session_id(payload, host)):
            codex_hooks.remove_refetch_state(sid, env)
        return handle_stop(host, dict(payload, hook_event_name=ev) if ev else payload, cfg, env)
    return {}


def main(argv: list[str] | None = None) -> int:
    _enforce_utf8()
    args = list(sys.argv[1:] if argv is None else argv)
    host_hint: str | None = None
    event = ""
    job_path = ""
    i = 0
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            host_hint = args[i + 1]
            i += 2
        elif args[i] == "--event" and i + 1 < len(args):
            event = args[i + 1]
            i += 2
        elif args[i] == "--job" and i + 1 < len(args):
            job_path = args[i + 1]
            i += 2
        else:
            i += 1

    if event == "_background_fold" and job_path:
        try:
            _run_background_fold(host_hint or "claude", job_path, dict(os.environ))
        except Exception:
            pass
        return 0

    if "--serve-worker" in args:
        key = ""
        if "--worker-key" in args and args.index("--worker-key") + 1 < len(args):
            key = args[args.index("--worker-key") + 1]
        try:
            from runtime import hook_worker

            return hook_worker.serve(key, _run_hook, _parse_payload_bytes)
        except Exception:
            return 0

    out = _run_hook(event, _read_stdin_json(), host_hint)
    if out:
        try:
            sys.stdout.write(out)
            sys.stdout.flush()
        except Exception:
            pass
    return 0


def _run_hook(event: str, payload: dict[str, Any], host_hint: str | None) -> str:
    """Dispatch one hook event against the current process environment; returns its stdout."""
    try:
        result = dispatch(event, payload, host_hint=host_hint, env=dict(os.environ))
        if result is not None:
            return json.dumps(result) + "\n"
    except Exception:
        # Fail-open contract: never crash or block the agent loop.
        try:
            log = state_home() / "errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            import traceback
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} event={event} host={host_hint}\n")
                fh.write(traceback.format_exc())
        except Exception:
            pass
    return ""


if __name__ == "__main__":
    sys.exit(main())
