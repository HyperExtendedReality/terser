#!/usr/bin/env python3
"""Token Optimizer — Google Antigravity hook bridge.

One fast, fail-open entry point for the wired Antigravity lifecycle events,
registered via ``~/.gemini/config/plugins/token-optimizer/hooks.json``:

  session-start   -> workspace-scoped continuity restore (once per session)
  pre-invocation  -> thrash nudges + context-fill nudges (and fallback restore)
  pre-tool-use    -> bash output compression, bounded read_file cache/delta,
                     and duplicate read-only MCP call guard
  post-tool-use   -> lightweight thrash/retry-loop streak tracking
  stop            -> detached rollup + dashboard regeneration

Design contract (R15): every handler exits 0 and emits EITHER one JSON object
or nothing, on any error, malformed stdin, oversize stdin (over 4 MB), or a
missing dependency. Consent-gated (R20): without the install record in
``~/.gemini/token-optimizer/config.json`` every handler no-ops to ``{}``.

Hot-path imports stay stdlib + the copied payload modules (runtime_env,
bash_hook, spawn_utils, antigravity_read_cache, antigravity_thrash).
``measure.py`` is never imported here — the Stop hook locates it through the
``measure-path`` locator written by the installer (KTD5, the Hermes installer
precedent).
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

try:
    from utf8_io import enforce_utf8_io  # noqa: PLC0415
except Exception:  # pragma: no cover

    def enforce_utf8_io() -> None:
        pass


_CONVERSATION_ID_RE = None
try:
    import re as _re

    _CONVERSATION_ID_RE = _re.compile(r"^[0-9a-f-]{1,64}$")
except Exception:  # pragma: no cover
    pass

_STDIN_MAX_BYTES = 4 * 1024 * 1024  # 4 MB (R15)
_RESTORE_MAX_BYTES = 16 * 1024  # 16 KB (R11)
_FIELD_MAX_CHARS = 200  # R22 per-field cap
_NUDGE_STATE_TTL_SECONDS = 7 * 86400
_ROLLUP_LEASE_SECONDS = 30

_SURFACES = ("antigravity-cli", "antigravity", "antigravity-ide")

_SCRIPT_DIR = Path(__file__).resolve().parent
_MEASURE_LOCATOR = _SCRIPT_DIR / "measure-path"


def _to_dir(home: Path) -> Path:
    return home / "token-optimizer"


def _consent_ok(home: Path) -> bool:
    """True when the installer recorded data consent."""
    config_path = _to_dir(home) / "config.json"
    try:
        if not config_path.is_file():
            return False
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        return bool(isinstance(cfg, dict) and cfg.get("antigravity_consent"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _clean_field(value, max_chars: int = _FIELD_MAX_CHARS) -> str:
    """Printable-only, single-line, length-capped text (R22)."""
    text = str(value or "")
    text = "".join(ch if ch == " " or ch.isprintable() else " " for ch in text)
    return " ".join(text.split())[:max_chars]


def _read_payload() -> dict | None:
    """Read the hook JSON payload from stdin with a 4 MB cap."""
    try:
        data = sys.stdin.buffer.read(_STDIN_MAX_BYTES + 1)
    except (OSError, AttributeError):
        return None
    if not data:
        return {}
    if len(data) > _STDIN_MAX_BYTES:
        return None
    try:
        obj = json.loads(data.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _emit(obj: dict) -> None:
    """Write exactly one JSON object to stdout."""
    try:
        sys.stdout.write(json.dumps(obj))
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


# ---------------------------------------------------------------------------
# SessionStart & PreInvocation
# ---------------------------------------------------------------------------


def _system_step(text: str, *, user_markdown: str | None = None) -> dict:
    """Build an Antigravity 2.0 systemMessage injectStep (replaces deprecated ephemeralMessage)."""
    body: dict = {"systemMessage": text}
    if user_markdown is not None:
        body["metadata"] = {"user_markdown": user_markdown}
    return {"systemMessage": body}


def _normalize_workspace(raw: str) -> str:
    """Normalize a workspace path or file:// URI for cross-platform matching."""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith("file://"):
        text = text[len("file://") :]
    text = urllib.parse.unquote(text)
    # Handle /C:/... URI form on Windows.
    if len(text) >= 3 and text[0] == "/" and text[2] == ":" and text[1].isalpha():
        text = text[1:]
    text = text.replace("\\", "/").rstrip("/")
    return text.lower() if (os.name == "nt" or (len(text) >= 2 and text[1] == ":")) else text


def _workspace_hash(workspace: str) -> str:
    norm = _normalize_workspace(workspace)
    if not norm:
        return ""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:12]


def _payload_workspace(payload: dict) -> str:
    ws_paths = payload.get("workspacePaths")
    if isinstance(ws_paths, list):
        for item in ws_paths:
            if isinstance(item, str) and item.strip():
                return item.strip()
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return cwd.strip()
    return ""


def _read_restore_file(restore_path: Path) -> str | None:
    try:
        if (
            not restore_path.is_file()
            or restore_path.stat().st_size > _RESTORE_MAX_BYTES
        ):
            return None
        content = restore_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not content:
        return None
    # Defense-in-depth: re-clean every line (printable, single-line, capped).
    cleaned = "\n".join(_clean_field(line) for line in content.splitlines())
    return cleaned.strip() or None


def _restore_matches_workspace(content: str, workspace: str) -> bool:
    """True if `content`'s Working dir matches `workspace` (or has no Working dir)."""
    target = _normalize_workspace(workspace)
    if not target:
        return True
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("- working dir:"):
            recorded = stripped.split(":", 1)[1].strip()
            return _normalize_workspace(recorded) == target
    return False


def _restore_message(home: Path, payload: dict | None = None) -> str | None:
    """Load continuity context scoped to the current workspace when known."""
    to_dir = _to_dir(home)
    workspace = _payload_workspace(payload) if isinstance(payload, dict) else ""
    if workspace:
        ws_hash = _workspace_hash(workspace)
        if ws_hash:
            scoped = _read_restore_file(to_dir / f"restore-context-{ws_hash}.md")
            if scoped:
                return scoped

    global_msg = _read_restore_file(to_dir / "restore-context.md")
    if not global_msg:
        return None
    if workspace and not _restore_matches_workspace(global_msg, workspace):
        return None
    return global_msg


def _surface_dir_for_transcript(home: Path, transcript_path) -> Path | None:
    """Resolve the surface directory from transcriptPath's ancestor directories.

    Antigravity 2.0 stores transcripts under:
      ``<home>/<surface>/brain/<conversation-id>/.system_generated/logs/transcript.jsonl``
    while older/CLI layouts may use ``<home>/<surface>/transcript.jsonl``.
    Walk ancestors to find the matching surface name inside ``home`` without
    ever opening or traversing ``transcript_path`` itself (KTD6 boundary).
    """
    try:
        raw = str(transcript_path or "")
        if not raw:
            return None
        for ancestor in Path(raw).parents:
            if ancestor.name in _SURFACES:
                surf = home / ancestor.name
                return surf if (surf.is_dir() and not surf.is_symlink()) else None
        return None
    except (OSError, ValueError):
        return None


def _live_fill(surface_dir: Path, conversation_id: str) -> float | None:
    try:
        from antigravity_state import read_live_conversation
    except Exception:
        return None
    db = surface_dir / "conversations" / f"{conversation_id}.db"
    session = read_live_conversation(db)
    if session is None:
        return None
    return session.get("last_fill")


def _current_fill_from_payload(home: Path, payload: dict) -> float | None:
    conversation_id = str(payload.get("conversationId") or "")
    if not _CONVERSATION_ID_RE or not _CONVERSATION_ID_RE.match(conversation_id):
        return None
    surface_dir = _surface_dir_for_transcript(home, payload.get("transcriptPath") or "")
    if surface_dir is None:
        return None
    return _live_fill(surface_dir, conversation_id)


def _nudge_state_path(home: Path, conversation_id: str) -> Path:
    return _to_dir(home) / "nudge-state" / f"{conversation_id}.json"


def _nudge_already_sent(home: Path, conversation_id: str, threshold: str) -> bool:
    path = _nudge_state_path(home, conversation_id)
    try:
        if not path.is_file():
            return False
        state = json.loads(path.read_text(encoding="utf-8"))
        return bool(isinstance(state, dict) and state.get(threshold))
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _record_nudge(home: Path, conversation_id: str, threshold: str) -> None:
    _sweep_nudge_state(home)
    path = _nudge_state_path(home, conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Two hook processes can fire for the same conversation concurrently; an
    # unlocked read-modify-write loses one writer's threshold key and the
    # nudge fires twice. Serialize the whole RMW on a sidecar lock file.
    lock_fd = None
    try:
        lock_fd = os.open(str(path.parent / f".{path.name}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except Exception:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
        lock_fd = None  # no lock available: proceed, best-effort
    try:
        state: dict = {}
        try:
            if path.is_file():
                state = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    state = {}
        except (OSError, json.JSONDecodeError, ValueError):
            state = {}
        state[threshold] = True
        fd, tmp = tempfile.mkstemp(prefix=".nudge.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            os.replace(tmp, str(path))
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    finally:
        if lock_fd is not None:
            try:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass


def _sweep_nudge_state(home: Path) -> None:
    ndir = _to_dir(home) / "nudge-state"
    try:
        if not ndir.is_dir():
            return
        cutoff = time.time() - _NUDGE_STATE_TTL_SECONDS
        for p in ndir.glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                continue
    except OSError:
        pass


def handle_session_start(payload: dict) -> dict:
    """Inject workspace-scoped continuity context once at session startup."""
    try:
        from runtime_env import antigravity_home
    except Exception:
        return {}
    home = antigravity_home()

    conversation_id = str(payload.get("conversationId") or "")
    if conversation_id and _nudge_already_sent(home, conversation_id, "restored"):
        return {}

    message = _restore_message(home, payload)
    if not message:
        return {}

    if conversation_id and _CONVERSATION_ID_RE and _CONVERSATION_ID_RE.match(conversation_id):
        _record_nudge(home, conversation_id, "restored")
    return {"injectSteps": [_system_step(message)]}


def handle_pre_invocation(payload: dict) -> dict:
    try:
        from runtime_env import antigravity_home
    except Exception:
        return {}
    home = antigravity_home()

    invocation_num = payload.get("invocationNum")
    try:
        invocation_num = int(invocation_num)
    except (TypeError, ValueError):
        invocation_num = 0

    conversation_id = str(payload.get("conversationId") or "")
    steps: list[dict] = []

    if invocation_num == 1:
        already = bool(
            conversation_id and _nudge_already_sent(home, conversation_id, "restored")
        )
        if not already:
            message = _restore_message(home, payload)
            if message:
                if conversation_id and _CONVERSATION_ID_RE and _CONVERSATION_ID_RE.match(conversation_id):
                    _record_nudge(home, conversation_id, "restored")
                steps.append(_system_step(message))
        return {"injectSteps": steps} if steps else {}

    if invocation_num <= 1:
        return {}

    # Surface any pending PostToolUse thrash/retry-loop nudge once.
    try:
        import antigravity_thrash as _thrash

        thrash_nudge = _thrash.pop_pending_nudge(home, conversation_id)
        if thrash_nudge:
            steps.append(_system_step(thrash_nudge))
    except Exception:
        pass

    # Fast-path: skip SQLite fill read on early turns when initialNumSteps is
    # explicitly small (context cannot be near 70% yet).
    raw_steps = payload.get("initialNumSteps")
    skip_fill = False
    if raw_steps is not None:
        try:
            if int(raw_steps) < 8 and invocation_num < 4:
                skip_fill = True
        except (TypeError, ValueError):
            pass

    if not skip_fill:
        fill = _current_fill_from_payload(home, payload)
        try:
            valid_fill = fill is not None and (0.0 <= fill <= 1.0)
        except (TypeError, ValueError):
            valid_fill = False

        if valid_fill:
            if fill >= 0.85:
                if not _nudge_already_sent(home, conversation_id, "85"):
                    _record_nudge(home, conversation_id, "85")
                    steps.append(
                        _system_step(
                            "[Token Optimizer] Context is above 85% full. Prefer small, focused "
                            "changes; use /compact or start a fresh conversation soon to avoid truncation."
                        )
                    )
            elif fill >= 0.70:
                if not _nudge_already_sent(home, conversation_id, "70"):
                    _record_nudge(home, conversation_id, "70")
                    steps.append(
                        _system_step(
                            "[Token Optimizer] Context is above 70% full. Keep prompts and tool outputs tight."
                        )
                    )

    return {"injectSteps": steps} if steps else {}


# ---------------------------------------------------------------------------
# PreToolUse
# ---------------------------------------------------------------------------


def _ps_quote(token: str) -> str:
    """Quote a token as a PowerShell literal single-quoted string."""
    return "'" + token.replace("'", "''") + "'"


def _split_powershell_command(command: str) -> list[str] | None:
    """Tokenize a simple PowerShell command without stripping backslashes.

    Assumes dangerous chars (including backtick and `$`) were already rejected.
    Supports single-quoted (`''` -> `'`) and double-quoted (`""` -> `"`)
    segments, including adjacent segments like `--format='%h %s'`.
    Returns None on unclosed quotes or empty input.
    """
    tokens: list[str] = []
    buf: list[str] = []
    in_token = False
    quote: str | None = None
    i = 0
    n = len(command)
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
                in_token = True
                i += 1
            elif ch.isspace():
                if in_token:
                    tokens.append("".join(buf))
                    buf = []
                    in_token = False
                i += 1
            else:
                buf.append(ch)
                in_token = True
                i += 1
    if quote is not None:
        return None
    if in_token:
        tokens.append("".join(buf))
    return tokens or None


def handle_pre_tool_use(payload: dict) -> dict:
    tool_call = payload.get("toolCall")
    if not isinstance(tool_call, dict):
        return {}
    tool_name = tool_call.get("name")
    args = tool_call.get("args")
    if not isinstance(args, dict):
        return {}

    if tool_name in ("read_file", "view_file"):
        try:
            from runtime_env import antigravity_home
            import antigravity_read_cache as _rc

            return _rc.check_read_file(antigravity_home(), payload)
        except Exception:
            return {}

    if isinstance(tool_name, str) and (tool_name == "call_mcp_tool" or tool_name.startswith("mcp_")):
        try:
            from runtime_env import antigravity_home
            import antigravity_read_cache as _rc

            return _rc.check_mcp_refetch(antigravity_home(), payload)
        except Exception:
            return {}

    if tool_name != "run_command":
        return {}
    if os.environ.get("TOKEN_OPTIMIZER_BASH_COMPRESS", "").strip() == "0":
        return {}

    command = args.get("CommandLine")
    if not isinstance(command, str) or not command:
        return {}

    try:
        import bash_hook as _bash_hook
    except Exception:
        return {}

    if _bash_hook._has_dangerous_chars(command):
        return {}

    compress_path = _SCRIPT_DIR / "bash_compress.py"
    if not compress_path.is_file():
        return {}

    if os.name == "nt":
        original_tokens = _split_powershell_command(command)
        if not original_tokens:
            return {}
        # POSIX `VAR=val cmd` prefixes are not valid PowerShell syntax.
        if "=" in original_tokens[0]:
            return {}
        # Only rewrite executables resolvable on PATH (skip PowerShell built-in
        # aliases like `ls` -> Get-ChildItem when no external binary exists).
        if shutil.which(original_tokens[0]) is None:
            return {}
        check_str = shlex.join(original_tokens)
        if _bash_hook._has_dangerous_chars(check_str) or not _bash_hook._is_whitelisted(check_str):
            return {}
        rewritten = (
            "& "
            + _ps_quote(sys.executable)
            + " -E -s "
            + _ps_quote(str(compress_path))
            + " "
            + " ".join(_ps_quote(t) for t in original_tokens)
        )
    else:
        if not _bash_hook._is_whitelisted(command):
            return {}
        try:
            original_tokens = shlex.split(command)
            if not original_tokens:
                return {}
        except ValueError:
            return {}
        check_str = command
        rewritten = (
            shlex.quote(sys.executable)
            + " -E -s "
            + shlex.quote(str(compress_path))
            + " "
            + " ".join(shlex.quote(t) for t in original_tokens)
        )

    # Default decision "ask" defers to the user's permission mode + "always
    # allow" cache (R13). Only when the user explicitly opts in via
    # TOKEN_OPTIMIZER_AUTO_APPROVE_READONLY=1 AND the command passes the
    # strict read-only gate (bash_whitelist.is_auto_approvable) do we emit
    # "allow". We never emit permissionOverrides.
    decision = "ask"
    if os.environ.get("TOKEN_OPTIMIZER_AUTO_APPROVE_READONLY", "").strip() == "1":
        try:
            import bash_whitelist as _bw

            if _bw.is_auto_approvable(check_str):
                decision = "allow"
        except Exception:
            decision = "ask"

    return {"decision": decision, "overwrite": {"CommandLine": rewritten}}


# ---------------------------------------------------------------------------
# PostToolUse
# ---------------------------------------------------------------------------


def handle_post_tool_use(payload: dict) -> dict:
    """Record tool call streaks for the thrash guard; always returns `{}`."""
    try:
        from runtime_env import antigravity_home
        import antigravity_thrash as _thrash

        _thrash.record_post_tool_use(antigravity_home(), payload)
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------


def _locate_measure_py() -> Path | None:
    if _MEASURE_LOCATOR.is_file():
        try:
            located = Path(_MEASURE_LOCATOR.read_text(encoding="utf-8").strip())
            if located.is_file():
                return located
        except (OSError, ValueError):
            pass
    sibling = _SCRIPT_DIR / "measure.py"
    if sibling.is_file():
        return sibling
    return None


def _rollup_lease_held(home: Path) -> bool:
    lease = _to_dir(home) / "rollup.lease"
    try:
        if not lease.exists():
            return False
        if (time.time() - lease.stat().st_mtime) < _ROLLUP_LEASE_SECONDS:
            return True
        return False
    except OSError:
        return False


def _touch_rollup_lease(home: Path) -> bool:
    """Touch the rollup lease; False when the write failed (degraded dir)."""
    lease = _to_dir(home) / "rollup.lease"
    try:
        lease.parent.mkdir(parents=True, exist_ok=True)
        lease.touch()
        return True
    except OSError:
        return False


def handle_stop(payload: dict) -> dict:
    try:
        from runtime_env import antigravity_home
    except Exception:
        return {}
    home = antigravity_home()
    if _rollup_lease_held(home):
        return {}

    measure_py = _locate_measure_py()
    if measure_py is None:
        return {}

    if not _touch_rollup_lease(home):
        # Fail closed (the Cursor bridge's throttle-state rule, R12): on a
        # read-only/full data dir the lease can never be observed held, so
        # without this guard EVERY stop would spawn a detached
        # rollup+dashboard pair - a process storm under exactly the degraded
        # conditions the lease exists to contain.
        return {}

    try:
        from spawn_utils import spawn_detached
    except Exception:
        return {}

    env = {
        **os.environ,
        "TOKEN_OPTIMIZER_RUNTIME": "antigravity",
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    try:
        spawn_detached(
            [sys.executable, str(measure_py), "antigravity-rollup", "--quiet"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    try:
        spawn_detached(
            [sys.executable, str(measure_py), "dashboard", "--quiet"],
            env={**env, "TOKEN_OPTIMIZER_INTERACTIVE": "1"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv=None) -> int:
    try:
        enforce_utf8_io()
    except Exception:
        pass

    # Pinned here as well as in the hook command: the Windows command cannot
    # carry an env prefix, and the bridge must never process-scan to guess.
    os.environ.setdefault("TOKEN_OPTIMIZER_RUNTIME", "antigravity")

    event = ""
    if argv:
        event = argv[0]
    elif len(sys.argv) > 1:
        event = sys.argv[1]

    payload = _read_payload() or {}

    try:
        from runtime_env import antigravity_home
    except Exception:
        return 0

    try:
        if not _consent_ok(antigravity_home()):
            if event != "pre-tool-use":
                _emit({})
            return 0

        if event == "session-start":
            out = handle_session_start(payload)
        elif event == "pre-invocation":
            out = handle_pre_invocation(payload)
        elif event == "pre-tool-use":
            out = handle_pre_tool_use(payload)
            # Antigravity's PreToolUse contract requires `decision` whenever
            # JSON is emitted; `{}` (missing decision) denies the tool call.
            # Exit 0 with empty stdout to pass through unchanged.
            if not out:
                return 0
        elif event == "post-tool-use":
            out = handle_post_tool_use(payload)
        elif event == "stop":
            out = handle_stop(payload)
        else:
            return 0
        _emit(out)
    except Exception:
        # Fail open: never break the Antigravity loop (R15). But never
        # silently: a hook that never fires is undiagnosable without a trace.
        # Guarded — a closed/full stderr must not turn fail-open into
        # fail-hung.
        try:
            import traceback
            traceback.print_exc(file=sys.stderr)
        except Exception:
            pass
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:2]))
