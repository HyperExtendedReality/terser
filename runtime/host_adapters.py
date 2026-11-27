"""Host adapters: run terser's existing hook handlers for opencode and Cursor.

Neither host gets its own handler tree. Each adapter does three things:

1. Translates the host's hook payload into the Claude Code shape that the
   handlers in ``runtime/unified_hook.py`` already understand (``tool_name``,
   ``tool_input``, ``tool_response``, ``session_id``, ``cwd``).
2. Calls ``unified_hook.dispatch`` with ``host_hint="claude"``.
3. Translates the Claude-shaped reply back into what the host accepts.

Anything the host cannot express is dropped. For example, Cursor cannot
rewrite shell output and has no user-only message channel. The adapters fail
open: any error yields the host's "allow / no change" reply.

Assumptions (the hosts' public docs leave these open; see README "Other hosts"):

opencode (https://opencode.ai/docs/plugins/)
  * The JS plugin in ``hooks/opencode-plugin.js`` (V1 plugin API) builds the
    payload itself, so terser defines that contract:
    ``{"event", "tool", "sessionID", "callID", "args", "output", "directory"}``.
  * Built-in tool names are ``bash``/``shell``, ``read``, ``write``, ``edit``,
    ``grep``, ``glob`` and ``webfetch``. Path args may be ``filePath`` (V1) or
    ``path`` (current docs), so both are accepted.
  * ``read`` offsets may be 0-based. Ranged reads therefore skip the
    read-dedup funnel; only whole-file reads are deduplicated.
  * MCP tool names have no reliable server prefix, so MCP tools pass through.

Cursor (https://cursor.com/docs/agent/hooks)
  * ``preToolUse`` and ``postToolUse`` carry ``tool_name``
    (``Shell``/``Read``/``Write``/``Grep``/``Delete``/``Task``/``MCP:<tool>``),
    ``tool_input`` and ``tool_output``. Inner arg names are not documented,
    so common spellings are accepted.
  * A permission hook that gets empty or invalid JSON back blocks the action,
    so ``preToolUse`` and ``subagentStart`` always answer
    ``{"permission": "allow"}`` unless terser denies.
  * ``postToolUse`` can only replace MCP output (``updated_mcp_tool_output``),
    and its value format is unconfirmed. That rewrite is off unless
    ``cursor_mcp_output_rewrite`` is set in config.
"""
from __future__ import annotations

from typing import Any, Callable

FOREIGN_HOSTS = ("opencode", "cursor")

Dispatch = Callable[..., "dict[str, Any] | None"]


def _first_str(d: dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _as_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _text_of(value: Any) -> str:
    """Flatten a host tool result (string, MCP content list or dict) to text."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text_of(v) for v in value)
    if isinstance(value, dict):
        for key in ("output", "stdout", "text", "content", "result"):
            if key in value:
                return _text_of(value[key])
    return ""


# ---------------------------------------------------------------------------
# Tool translation (host tool + args -> Claude tool + tool_input)
# ---------------------------------------------------------------------------

def _translate_tool(host: str, tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    name = tool.strip()
    low = name.lower()
    if host == "cursor" and name.startswith("MCP:"):
        return f"mcp__cursor__{name[4:].strip()}", dict(args)
    if low in ("bash", "shell"):
        cmd = _first_str(args, "command", "cmd")
        return ("Bash", {"command": cmd}) if cmd else None
    if low == "read":
        fp = _first_str(args, "file_path", "filePath", "path", "target_file")
        if not fp:
            return None
        offset = _as_int(args.get("offset"))
        limit = _as_int(args.get("limit"))
        if offset is not None or limit is not None:
            return None  # 0- vs 1-based offsets are unconfirmed: never dedupe a ranged read.
        return "Read", {"file_path": fp}
    if low == "write":
        fp = _first_str(args, "file_path", "filePath", "path", "target_file")
        content = args.get("content", args.get("contents"))
        if fp and isinstance(content, str):
            return "Write", {"file_path": fp, "content": content}
        return None
    if low == "edit":
        fp = _first_str(args, "file_path", "filePath", "path")
        old = args.get("oldString", args.get("old_string"))
        new = args.get("newString", args.get("new_string"))
        if fp and isinstance(old, str) and isinstance(new, str):
            return "Edit", {"file_path": fp, "old_string": old, "new_string": new}
        return None
    if low == "grep":
        pattern = _first_str(args, "pattern", "query", "regex")
        if not pattern:
            return None
        out: dict[str, Any] = {"pattern": pattern}
        if path := _first_str(args, "path", "directory"):
            out["path"] = path
        if include := _first_str(args, "include", "glob"):
            out["glob"] = include
        return "Grep", out
    if low == "glob":
        pattern = _first_str(args, "pattern", "glob_pattern")
        if not pattern:
            return None
        out = {"pattern": pattern}
        if path := _first_str(args, "path", "directory"):
            out["path"] = path
        return "Glob", out
    if low == "webfetch":
        url = _first_str(args, "url")
        return ("WebFetch", {"url": url}) if url else None
    return None


def _host_cmd_key(args: dict[str, Any]) -> str:
    return "cmd" if "cmd" in args and "command" not in args else "command"


# ---------------------------------------------------------------------------
# Claude-shaped reply readers
# ---------------------------------------------------------------------------

def _hso(result: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    h = result.get("hookSpecificOutput")
    return h if isinstance(h, dict) else {}


def _deny_reason(result: dict[str, Any] | None) -> str | None:
    h = _hso(result)
    if h.get("permissionDecision") == "deny":
        return str(h.get("permissionDecisionReason") or "Blocked by terser.")
    return None


def _updated_output_text(result: dict[str, Any] | None) -> str | None:
    h = _hso(result)
    upd = h.get("updatedToolOutput", h.get("updatedMCPToolOutput"))
    if upd is None:
        return None
    if isinstance(upd, dict):
        stdout = upd.get("stdout")
        stderr = upd.get("stderr")
        if isinstance(stdout, str) or isinstance(stderr, str):
            parts = [p for p in (stdout, stderr) if isinstance(p, str) and p]
            return "\n".join(parts)
    text = _text_of(upd)
    return text if text else None


# ---------------------------------------------------------------------------
# opencode
# ---------------------------------------------------------------------------

_OPENCODE_EVENTS = {
    "tool.execute.before": "PreToolUse",
    "tool.execute.after": "PostToolUse",
    "session.created": "SessionStart",
    "session.idle": "Stop",
    "session.compacted": "PreCompact",
    "system.transform": "SessionStart",
}


def _opencode(event: str, payload: dict[str, Any], env: dict[str, str], inner: Dispatch) -> dict[str, Any]:
    ev = _OPENCODE_EVENTS.get(event or str(payload.get("event") or ""), event)
    base: dict[str, Any] = {
        "session_id": _first_str(payload, "sessionID", "session_id"),
        "cwd": _first_str(payload, "directory", "cwd"),
        "hook_event_name": ev,
    }
    if ev == "SessionStart":
        res = inner(ev, dict(base, source="startup"), host_hint="claude", env=env) or {}
        ctx = _hso(res).get("additionalContext")
        return {"context": ctx} if isinstance(ctx, str) and ctx else {}
    if ev in ("Stop", "PreCompact"):
        inner(ev, base, host_hint="claude", env=env)
        return {}
    if ev not in ("PreToolUse", "PostToolUse"):
        return {}

    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    mapped = _translate_tool("opencode", str(payload.get("tool") or ""), args)
    if not mapped:
        return {}
    tool_name, tool_input = mapped
    base.update(tool_name=tool_name, tool_input=tool_input, tool_use_id=_first_str(payload, "callID"))

    if ev == "PreToolUse":
        res = inner("PreToolUse", base, host_hint="claude", env=env)
        if reason := _deny_reason(res):
            return {"block": reason}
        upd = _hso(res).get("updatedInput")
        if tool_name == "Bash" and isinstance(upd, dict) and isinstance(upd.get("command"), str):
            return {"args": {_host_cmd_key(args): upd["command"]}}
        return {}

    raw = _text_of(payload.get("output"))
    base["tool_response"] = {"stdout": raw, "stderr": ""} if tool_name == "Bash" else raw
    if payload.get("error"):
        base["error"] = str(payload.get("error"))
    res = inner("PostToolUse", base, host_hint="claude", env=env)
    out: dict[str, Any] = {}
    new_text = _updated_output_text(res)
    if new_text is not None and new_text != raw:
        out["output"] = new_text
    ctx = _hso(res).get("additionalContext")
    if isinstance(ctx, str) and ctx:
        out["output"] = f"{out.get('output', raw).rstrip()}\n\n{ctx}"
    return out


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

_CURSOR_EVENTS = {
    "sessionstart": "SessionStart",
    "pretooluse": "PreToolUse",
    "posttooluse": "PostToolUse",
    "posttoolusefailure": "PostToolUseFailure",
    "subagentstart": "SubagentStart",
    "subagentstop": "SubagentStop",
    "precompact": "PreCompact",
    "stop": "Stop",
    "sessionend": "SessionEnd",
}
_CURSOR_PERMISSION_EVENTS = frozenset({"PreToolUse", "SubagentStart"})


def _cursor_default(ev: str) -> dict[str, Any]:
    return {"permission": "allow"} if ev in _CURSOR_PERMISSION_EVENTS else {}


def _cursor(event: str, payload: dict[str, Any], env: dict[str, str], inner: Dispatch) -> dict[str, Any]:
    raw_ev = event or str(payload.get("hook_event_name") or "")
    ev = _CURSOR_EVENTS.get(raw_ev.lower().replace("_", "").replace("-", ""), "")
    if not ev:
        return {}
    roots = payload.get("workspace_roots")
    cwd = _first_str(payload, "cwd") or (
        next((r for r in roots if isinstance(r, str) and r), "") if isinstance(roots, list) else ""
    )
    base: dict[str, Any] = {
        "session_id": _first_str(payload, "conversation_id", "session_id"),
        "cwd": cwd,
        "hook_event_name": ev,
    }
    # transcript_path is deliberately dropped: Cursor's transcript format is not Claude's JSONL.
    if sub := _first_str(payload, "subagent_id"):
        base["agent_id"] = sub

    if ev == "SessionStart":
        res = inner(ev, dict(base, source="startup"), host_hint="claude", env=env) or {}
        ctx = _hso(res).get("additionalContext")
        return {"additional_context": ctx} if isinstance(ctx, str) and ctx else {}
    if ev == "SubagentStart":
        return {"permission": "allow"}
    if ev in ("SubagentStop", "PreCompact", "Stop", "SessionEnd"):
        inner(ev, base, host_hint="claude", env=env)
        return {}  # never send followup_message: it would make the agent keep going.

    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    tool = str(payload.get("tool_name") or "")
    mapped = _translate_tool("cursor", tool, args)
    if not mapped:
        return _cursor_default(ev)
    tool_name, tool_input = mapped
    base.update(tool_name=tool_name, tool_input=tool_input, tool_use_id=_first_str(payload, "tool_use_id"))

    if ev == "PreToolUse":
        res = inner("PreToolUse", base, host_hint="claude", env=env)
        if reason := _deny_reason(res):
            return {"permission": "deny", "agent_message": reason, "user_message": "terser: " + reason.splitlines()[0][:160]}
        upd = _hso(res).get("updatedInput")
        if tool_name == "Bash" and isinstance(upd, dict) and isinstance(upd.get("command"), str):
            return {"permission": "allow", "updated_input": dict(args, **{_host_cmd_key(args): upd["command"]})}
        return {"permission": "allow"}

    failed = ev == "PostToolUseFailure"
    raw = _text_of(payload.get("tool_output"))
    base["tool_response"] = {"stdout": raw, "stderr": ""} if tool_name == "Bash" else raw
    if failed:
        base["error"] = _first_str(payload, "error_message") or "failed"
    res = inner(ev, base, host_hint="claude", env=env)
    out: dict[str, Any] = {}
    h = _hso(res)
    ctx = h.get("additionalContext")
    if isinstance(ctx, str) and ctx:
        out["additional_context"] = ctx
    if not failed and tool_name.startswith("mcp__") and isinstance(h.get("updatedMCPToolOutput"), str):
        from runtime.config import load_config

        if load_config(env).get("cursor_mcp_output_rewrite"):
            out["updated_mcp_tool_output"] = h["updatedMCPToolOutput"]
    return out


def dispatch_foreign(
    host: str, event: str, payload: dict[str, Any], env: dict[str, str], inner: Dispatch,
) -> dict[str, Any]:
    """Run one opencode/Cursor hook event through terser. Never raises."""
    try:
        if host == "opencode":
            return _opencode(event, payload, env, inner)
        if host == "cursor":
            return _cursor(event, payload, env, inner)
    except Exception:
        pass
    if host == "cursor":
        low = (event or str(payload.get("hook_event_name") or "")).lower()
        return {"permission": "allow"} if low in ("pretooluse", "subagentstart") else {}
    return {}
