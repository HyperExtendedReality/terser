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

from runtime import cmd_delta, index_bridge, loop_guard, output_compressor, read_funnel, session_brief
from runtime.config import (
    atomic_write_json,
    ensure_vendor_paths,
    load_config,
    state_home,
)

_STDIN_MAX_BYTES = 5 * 1024 * 1024  # 5 MB safety cap
_ROLLUP_LEASE_SECONDS = 30.0


def _enforce_utf8() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if callable(reconf):
            try:
                reconf(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _read_stdin_json() -> dict[str, Any]:
    try:
        buf = getattr(sys.stdin, "buffer", None)
        if buf is not None:
            raw = buf.read(_STDIN_MAX_BYTES + 1)
            if not raw or len(raw) > _STDIN_MAX_BYTES:
                return {}
            text = raw.decode("utf-8", errors="replace")
        else:
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
    "batch parallel reads; scope CLI output; return a concise summary with exact file paths and line spans—never dump raw files."
)
_SUBAGENT_BRIEF_CC = (
    "[Terser Subagent] Prefer `terser-index` (`order`/`route`, if available) or ranged reads (`offset`/`limit`); "
    "batch parallel reads; scope CLI output; return a concise summary with exact file paths and line spans—never dump raw files."
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


# ---------------------------------------------------------------------------
# Shell Command Compression Rewrite Helpers (Antigravity PreToolUse)
# ---------------------------------------------------------------------------

def _ps_quote(token: str) -> str:
    return "'" + token.replace("'", "''") + "'"


def _split_powershell_command(command: str) -> list[str] | None:
    tokens: list[str] = []
    buf: list[str] = []
    in_tok = False
    quote: str | None = None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            if ch == quote:
                if i + 1 < n and command[i + 1] == quote:
                    buf.append(quote)
                    i += 2
                    continue
                quote = None
                i += 1
                continue
            buf.append(ch)
            i += 1
        else:
            if ch in ("'", '"'):
                quote = ch
                in_tok = True
                i += 1
            elif ch.isspace():
                if in_tok:
                    tokens.append("".join(buf))
                    buf = []
                    in_tok = False
                i += 1
            else:
                buf.append(ch)
                in_tok = True
                i += 1
    if quote is not None:
        return None
    if in_tok:
        tokens.append("".join(buf))
    return tokens or None


def _wrap_command_for_compression(
    command: str,
    cfg: dict[str, Any],
    *,
    session_id: str = "",
) -> tuple[str | None, bool]:
    """If `command` is safe & whitelisted, wrap it in output_compressor.py.

    Returns ``(rewritten_command_or_None, is_auto_approvable_readonly)``.
    """
    if not cfg.get("bash_compress", True) or cfg.get("shadow"):
        return None, False
    try:
        ensure_vendor_paths()
        import bash_hook as _bh  # type: ignore
    except Exception:
        return None, False

    if _bh._has_dangerous_chars(command):
        return None, False

    compressor = _THIS_DIR / "output_compressor.py"
    if not compressor.is_file():
        return None, False

    fast_bin = _PLUGIN_ROOT / "bin" / ("terser-fast.exe" if os.name == "nt" else "terser-fast")
    safe_sid = "".join(c for c in (session_id or "") if c.isalnum() or c in "-_")[:64] if cfg.get("cmd_delta", True) else ""

    def _prefer_native_exec(toks: list[str]) -> bool:
        if not fast_bin.is_file() or not toks:
            return False
        head = Path(toks[0]).stem.lower()
        sub = toks[1].lower() if len(toks) > 1 else ""
        # Use ~3ms native Rust exec for build/pkg/diff commands (no Python startup overhead)
        return head in ("cargo", "go", "npm", "pnpm", "yarn", "pip", "uv", "tsc") or (head == "git" and sub == "diff")

    if os.name == "nt":
        tokens = _split_powershell_command(command)
        if not tokens or "=" in tokens[0]:
            return None, False
        if shutil.which(tokens[0]) is None:
            return None, False
        check_str = shlex.join(tokens)
        if _bh._has_dangerous_chars(check_str) or not _bh._is_whitelisted(check_str):
            return None, False
        if _prefer_native_exec(tokens):
            rewritten = (
                "& "
                + _ps_quote(str(fast_bin))
                + " exec --host antigravity -- "
                + " ".join(_ps_quote(t) for t in tokens)
            )
        else:
            sid_prefix = f"--sid {_ps_quote(safe_sid)} -- " if safe_sid else ""
            rewritten = (
                "& "
                + _ps_quote(sys.executable)
                + " -E -s "
                + _ps_quote(str(compressor))
                + " "
                + sid_prefix
                + " ".join(_ps_quote(t) for t in tokens)
            )
    else:
        if not _bh._is_whitelisted(command):
            return None, False
        try:
            tokens = shlex.split(command)
            if not tokens:
                return None, False
        except ValueError:
            return None, False
        check_str = command
        if _prefer_native_exec(tokens):
            rewritten = (
                shlex.quote(str(fast_bin))
                + " exec --host antigravity -- "
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
            import bash_whitelist as _bw  # type: ignore

            readonly_ok = bool(_bw.is_auto_approvable(check_str))
        except Exception:
            readonly_ok = False

    return rewritten, readonly_ok


# ---------------------------------------------------------------------------
# Session-scoped once-marker & live context fill (Antigravity)
# ---------------------------------------------------------------------------

def _session_flag_path(session_id: str, env: dict[str, str]) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:80]
    return state_home(env) / "session-flags" / f"{safe}.json"


def _has_flag(session_id: str, key: str, env: dict[str, str]) -> bool:
    if not session_id:
        return False
    p = _session_flag_path(session_id, env)
    try:
        if not p.is_file():
            return False
        data = json.loads(p.read_text(encoding="utf-8"))
        return bool(isinstance(data, dict) and data.get(key))
    except Exception:
        return False


def _set_flag(session_id: str, key: str, env: dict[str, str]) -> None:
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
    data[key] = True
    atomic_write_json(p, data)


def _antigravity_live_fill(payload: dict[str, Any], env: dict[str, str]) -> float | None:
    try:
        ensure_vendor_paths()
        from antigravity_state import read_live_conversation  # type: ignore
        from runtime_env import antigravity_home  # type: ignore

        cid = str(payload.get("conversationId") or "")
        tp = str(payload.get("transcriptPath") or "")
        if not cid or not tp:
            return None
        home = antigravity_home()
        for anc in Path(tp).parents:
            if anc.name in ("antigravity", "antigravity-cli", "antigravity-ide"):
                db = home / anc.name / "conversations" / f"{cid}.db"
                sess = read_live_conversation(db)
                return sess.get("last_fill") if isinstance(sess, dict) else None
    except Exception:
        return None
    return None


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
    child_env["TOKEN_OPTIMIZER_RUNTIME"] = host
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

    # 1. Session micro-skills fold (updates EnvFacts/microskills + toolprune ledger)
    try:
        from tokeneconomizer.plugin import hook as te_hook  # type: ignore

        ev = dict(payload)
        ev.setdefault("hook_event_name", "SessionEnd" if host in ("claude", "codex") else "Stop")
        te_hook.run(ev, env, host_hint=host)
    except Exception:
        pass

    # 2. Analytics rollup / checkpoint / dashboard refresh
    measure_py = _PLUGIN_ROOT / "vendor" / "token_optimizer" / "measure.py"
    if measure_py.is_file():
        child_env = dict(env)
        child_env["TOKEN_OPTIMIZER_RUNTIME"] = host
        child_env["PYTHONUTF8"] = "1"
        from runtime.config import no_window_kwargs

        nw = no_window_kwargs()
        try:
            if host == "antigravity":
                subprocess.run(
                    [sys.executable, "-E", "-s", str(measure_py), "antigravity-rollup", "--quiet"],
                    env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=45, **nw,
                )
                subprocess.run(
                    [sys.executable, "-E", "-s", str(measure_py), "dashboard", "--quiet"],
                    env={**child_env, "TOKEN_OPTIMIZER_INTERACTIVE": "1"},
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, **nw,
                )
            else:
                trigger = str(payload.get("_terser_trigger") or "stop")
                args = [sys.executable, "-E", "-s", str(measure_py), "compact-capture", "--trigger", trigger, "--quiet"]
                subprocess.run(
                    args, input=json.dumps(payload), text=True,
                    env=child_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, **nw,
                )
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
        read_funnel.clear_session_cache(sid, env)

    if sid and _has_flag(sid, "brief_sent", env) and source == "startup":
        return {}

    brief, notice = session_brief.build_session_brief(ws, host=host, cfg=cfg, env=env)
    if sid and brief:
        _set_flag(sid, "brief_sent", env)

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
    nudge = loop_guard.pop_pending_nudge(sid, cfg=cfg, env=env)
    if nudge:
        return {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": nudge,
            }
        }
    return {}


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
                    return {"decision": "allow", "overwrite": {"Prompt": f"{prompt.rstrip()}\n\n{_SUBAGENT_BRIEF_AG}"}}
            elif tool_name == "swarm":
                action = str(args.get("action") or "").strip().lower()
                brief = args.get("brief")
                if (
                    action == "add"
                    and isinstance(brief, str)
                    and brief.strip()
                    and "[Terser Subagent]" not in brief
                ):
                    return {"decision": "allow", "overwrite": {"brief": f"{brief.rstrip()}\n\n{_SUBAGENT_BRIEF_AG}"}}
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
    if tool_name == "Grep" and cfg.get("index_grep_answer", True) and not cfg.get("shadow"):
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

    # 3. Bash/Shell -> FormRepair via updatedInput (Claude & Codex)
    if tool_name in ("Bash", "bash", "shell", "exec_command"):
        cmd_key = "command" if "command" in tool_input else ("cmd" if "cmd" in tool_input else "command")
        cmd = tool_input.get(cmd_key)
        if isinstance(cmd, str) and cmd.strip():
            repaired, was_repaired = loop_guard.apply_form_repair(ws, cmd, cfg=cfg, host=host, env=env)
            if was_repaired:
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

    return None


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

        # Edit/Write -> reset read-cache soft_denies + fire-and-forget AST reindex + continuity event
        ws = _workspace(payload, env)
        if tool_name in ("edit_file", "write_file"):
            fp = str(args.get("Path") or args.get("AbsolutePath") or args.get("path") or "")
            if fp:
                read_funnel.invalidate_on_edit(sid, fp, env=env)
                if cfg.get("index_auto_reindex", True):
                    index_bridge.spawn_reindex_file(fp, env=env)
                if ws and not cfg.get("shadow"):
                    from runtime import context_sandbox

                    context_sandbox.record_session_event(sid, ws, "edit", Path(fp).name, env=env)

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
    is_err = is_failure_event or bool(payload.get("error"))

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
            for m in _re.finditer(r"^\*\*\* (?:Update|Add|Delete) File:\s*(.+?)\s*$", patch_text, _re.M):
                p = m.group(1).strip()
                if p and p not in edited_paths:
                    edited_paths.append(p)
        ws_cl = _workspace(payload, env)
        for fp in edited_paths:
            read_funnel.invalidate_on_edit(sid, fp, env=env)
            if cfg.get("index_auto_reindex", True):
                index_bridge.spawn_reindex_file(fp)
            if ws_cl and not cfg.get("shadow"):
                from runtime import context_sandbox

                context_sandbox.record_session_event(sid, ws_cl, "edit", Path(fp).name, env=env)

    hso: dict[str, Any] = {}

    # 2. Invisible PostToolUse Bash output compression + cross-run Command Delta (Claude Code)
    if (
        host == "claude"
        and tool_name == "Bash"
        and cfg.get("bash_compress", True)
        and not cfg.get("shadow")
        and isinstance(tool_resp, dict)
    ):
        stdout = tool_resp.get("stdout")
        if isinstance(stdout, str) and len(stdout) > 300:
            cmd = str(tool_input.get("command") or "")
            comp = stdout
            stats = {"raw_chars": len(stdout), "served_chars": len(stdout)}
            if len(stdout) > 1500:
                comp, stats = output_compressor.compress_output(
                    stdout, command=cmd, enhanced=bool(cfg.get("enhanced_compression", True)),
                    host=host, env=env,
                )
            if cfg.get("cmd_delta", True):
                comp, delta_used = cmd_delta.apply_command_delta(
                    sid, cmd, comp, returncode=1 if is_err else 0, host=host, env=env,
                )
                if delta_used:
                    stats["served_chars"] = len(comp)
            if stats["served_chars"] < int(stats["raw_chars"] * 0.85):
                updated_resp = dict(tool_resp, stdout=comp)
                hso["hookEventName"] = "PostToolUse"
                hso["updatedToolOutput"] = updated_resp

    # 3. Oversized MCP output compression + archival (Claude Code)
    if (
        host == "claude"
        and tool_name.startswith("mcp__")
        and cfg.get("archive_large_outputs", True)
        and not cfg.get("shadow")
    ):
        raw_text = tool_resp if isinstance(tool_resp, str) else json.dumps(tool_resp, default=str) if tool_resp is not None else ""
        threshold = int(cfg.get("archive_threshold_bytes", 16384))
        if len(raw_text) > threshold:
            comp, stats = output_compressor.compress_output(
                raw_text, command=tool_name, max_chars=threshold // 2,
                enhanced=bool(cfg.get("enhanced_compression", True)),
                host=host, env=env,
            )
            if stats["served_chars"] < int(stats["raw_chars"] * 0.85):
                hso["hookEventName"] = "PostToolUse"
                hso["updatedMCPToolOutput"] = comp

    # 3b. Runaway Glob / Grep output guard (Claude Code): cap huge scans + FTS5 archive
    if (
        host == "claude"
        and tool_name in ("Glob", "Grep")
        and cfg.get("archive_large_outputs", True)
        and not cfg.get("shadow")
    ):
        raw_text = tool_resp if isinstance(tool_resp, str) else json.dumps(tool_resp, default=str) if tool_resp is not None else ""
        lines = raw_text.splitlines()
        if len(raw_text) > 8192 and len(lines) > 120:
            aid = output_compressor._archive_full_output(raw_text, command=tool_name, env=env)
            ptr = f' (`terse query {aid} "<terms>"` or `terse expand {aid} 1 100`)' if aid else ""
            capped = "\n".join(lines[:60]) + f"\n... [{len(lines) - 60} more {tool_name} lines omitted{ptr}]"
            hso["hookEventName"] = "PostToolUse"
            hso["updatedToolOutput"] = capped

    # 4. Unified Loop/Thrash Guard + live error continuity event
    if is_err and not cfg.get("shadow"):
        ws_err = _workspace(payload, env)
        if ws_err:
            from runtime import context_sandbox

            err_snip = str(payload.get("error") or "")[:120]
            context_sandbox.record_session_event(sid, ws_err, "error", f"{tool_name}: {err_snip}", env=env)
    nudge = loop_guard.record_tool_event(sid, tool_name, tool_input, is_error=is_err, cfg=cfg, env=env)
    if nudge:
        hso["hookEventName"] = "PostToolUseFailure" if is_failure_event else "PostToolUse"
        hso["additionalContext"] = nudge

    return {"hookSpecificOutput": hso} if hso else {}


def handle_pre_compact(host: str, payload: dict[str, Any], cfg: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    sid = _session_id(payload, host)
    read_funnel.clear_session_cache(sid, env)
    _trigger_background_fold(host, dict(payload, _terser_trigger="auto"), env)
    # On Claude Code PreCompact, emit dynamic compaction preservation instructions
    # so active files, decisions, and errors survive compaction without re-reads.
    if host == "claude" and not cfg.get("shadow"):
        measure_py = _PLUGIN_ROOT / "vendor" / "token_optimizer" / "measure.py"
        if measure_py.is_file():
            try:
                from runtime.config import no_window_kwargs

                proc = subprocess.run(
                    [sys.executable, "-E", "-s", str(measure_py), "dynamic-compact-instructions", "--quiet"],
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=dict(env, TOKEN_OPTIMIZER_RUNTIME="claude", PYTHONUTF8="1"),
                    timeout=8,
                    **no_window_kwargs(),
                )
                raw = (proc.stdout or "").strip()
                if raw.startswith("{"):
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        return parsed
                elif raw:
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreCompact",
                            "additionalContext": raw,
                        }
                    }
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
    return {}


def handle_stop(host: str, payload: dict[str, Any], cfg: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    if not cfg.get("shadow"):
        _trigger_background_fold(host, payload, env)
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
    cfg = load_config(env)
    host = _detect_host(payload, env, host_hint)

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
    if low in ("precompact", "postcompact", "cwdchanged"):
        return handle_pre_compact(host, payload, cfg, env)
    if low == "subagentstart":
        return handle_subagent_start(host, payload, cfg, env)
    if low == "subagentstop":
        return handle_subagent_stop(host, payload, cfg, env)
    if low in ("stop", "sessionend", "stopfailure"):
        return handle_stop(host, payload, cfg, env)
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

    try:
        payload = _read_stdin_json()
        result = dispatch(event, payload, host_hint=host_hint, env=dict(os.environ))
        if result is not None:
            sys.stdout.write(json.dumps(result) + "\n")
            sys.stdout.flush()
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
