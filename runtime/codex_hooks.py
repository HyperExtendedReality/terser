"""Codex-only hook wire format and conservative MCP result reuse."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterator

from runtime.config import atomic_write_json, record_savings, state_home


_STATIC_TOOL = re.compile(r"mcp__(?:plugin_terser_)?terser[-_]index__(menu|route)")
_INDEX_ORDER = re.compile(r"mcp__(?:plugin_terser_)?terser[-_]index__order")
_TRANSCRIPT_TAIL_BYTES = 512 * 1024
_LITERAL_COMMAND = re.compile(r"[ \t]*(?P<exe>[A-Za-z0-9_./:-]+)(?P<args>[ \t].*)?")


def equivalent_command(original: str, repaired: str, cwd: str, env: dict[str, str] | None) -> bool:
    """Accept executable spelling changes that preserve the file, name, and argument text."""
    old, new = _LITERAL_COMMAND.fullmatch(original), _LITERAL_COMMAND.fullmatch(repaired)
    if not old or not new or old["args"] != new["args"]:
        return False

    def resolve(token: str) -> str | None:
        if ":" in token and not Path(token).is_absolute():
            return None
        if "/" in token:
            return str(Path(cwd) / token)
        search = os.environ if env is None else env
        # Resolve relative PATH components in the command's working directory.
        path = os.pathsep.join(str(Path(cwd) / part) for part in search.get("PATH", os.defpath).split(os.pathsep))
        return shutil.which(token, path=path)

    def executable_name(token: str) -> str:
        name = Path(token).name
        return name.lower().removesuffix(".exe") if os.name == "nt" else name

    if executable_name(old["exe"]) != executable_name(new["exe"]):
        return False
    old_path, new_path = resolve(old["exe"]), resolve(new["exe"])
    if not old_path or not new_path:
        return False
    try:
        return os.path.isfile(old_path) and os.access(new_path, os.X_OK) and os.path.samefile(old_path, new_path)
    except OSError:
        return False


def allow_command(tool_input: dict[str, Any], command: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": dict(tool_input, command=command),
    }}


def tool_failed(payload: dict[str, Any]) -> bool:
    if payload.get("error"):
        return True
    response = payload.get("tool_response")
    if isinstance(response, dict):
        return bool(response.get("error") or response.get("isError") or response.get("success") is False
                    or response.get("exit_code") or response.get("returncode"))
    if isinstance(response, str) and payload.get("tool_name") == "Bash":
        return bool(re.search(r"(?m)^(?:Process exited with code|Exit code:) [1-9]\d*\b", response[:512]))
    return False


def _rollout_tail(transcript: str) -> Iterator[dict[str, Any]]:
    with Path(transcript).open("rb") as handle:
        handle.seek(0, 2)
        start = max(0, handle.tell() - _TRANSCRIPT_TAIL_BYTES)
        handle.seek(start)
        data = handle.read(_TRANSCRIPT_TAIL_BYTES)
    lines = data.split(b"\n")
    for raw in reversed(lines[1 if start else 0:]):
        if raw.strip():
            yield json.loads(raw)


def _compacted(record: dict[str, Any], item: dict[str, Any]) -> bool:
    return record.get("type") == "compacted" or item.get("type") in {"context_compacted", "history_replaced"}


def instruction_lines(transcript: object) -> tuple[str, ...]:
    """Get policy lines verifiably present in the current Codex context."""
    if not isinstance(transcript, str) or not transcript:
        return ()
    known: list[str] = []
    try:
        for record in _rollout_tail(transcript):
            item = record.get("payload") or {}
            if _compacted(record, item):
                break
            if record.get("type") != "response_item" or item.get("type") != "message":
                continue
            role = item.get("role")
            for part in item.get("content", []):
                text = part.get("text", "")
                if role == "user" and text.startswith("# AGENTS.md instructions for ") and "<INSTRUCTIONS>" in text:
                    body, closed, _ = text.split("<INSTRUCTIONS>", 1)[1].partition("</INSTRUCTIONS>")
                    if closed:
                        known.extend(body.splitlines())
                elif role == "developer" and text.startswith("[Terser Brief]\n"):
                    known.extend(text.splitlines()[1:])
                elif role == "developer":
                    for block in re.finditer(r"<skills_instructions>(.*?)</skills_instructions>", text, re.S):
                        known.extend(block[1].splitlines())
    except (OSError, ValueError, TypeError, AttributeError):
        return ()
    return tuple(known)


def _was_delivered(transcript: str, use_id: str, expected: str) -> bool:
    """Confirm the model-facing result, since PostToolUse runs before delivery."""
    try:
        for record in _rollout_tail(transcript):
            item = record.get("payload") or {}
            if _compacted(record, item):
                return False
            if record.get("type") != "response_item" or item.get("call_id") != use_id:
                continue
            if item.get("type") not in {"function_call_output", "mcp_tool_call_output"}:
                continue
            output = item.get("output")
            if isinstance(output, dict):
                if output.get("isError") or output.get("structuredContent") is not None:
                    return False
                output = output.get("content")
            if isinstance(output, list) and len(output) == 1 and isinstance(output[0], dict):
                if output[0].get("type") in {"text", "input_text"}:
                    output = output[0].get("text")
            return output == expected
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return False


def mcp_refetch(
    sid: str, payload: dict[str, Any], cfg: dict[str, Any], env: dict[str, str],
    *, completed: bool,
) -> dict[str, Any] | None:
    """Reuse only the exact same-turn evidence demonstrably delivered to Codex."""
    if cfg.get("shadow") or not cfg.get("mcp_refetch_guard"):
        return None
    tool = str(payload.get("tool_name") or "")
    static = _STATIC_TOOL.fullmatch(tool)
    turn, use_id = payload.get("turn_id"), payload.get("tool_use_id")
    args = payload.get("tool_input")
    transcript = payload.get("transcript_path")
    if not (sid and isinstance(turn, str) and turn and isinstance(use_id, str) and use_id
            and isinstance(transcript, str) and transcript and isinstance(args, dict)):
        return None
    inner = args.get("args")
    source = (
        _INDEX_ORDER.fullmatch(tool) is not None and args.get("action") == "get_symbol_source"
        and isinstance(inner, dict) and inner.get("reuse_visible_result") is True
    )
    if not (source or (static and cfg.get("codex_mcp_refetch_guard"))):
        return None
    from runtime import index_mcp

    try:
        if source:
            # A blank repo can resolve differently in a long-lived MCP process.
            if not isinstance(inner.get("repo"), str) or not inner["repo"].strip():
                return None
            action = "get_symbol_source"
            result = index_mcp.dispatch_action(action, inner, env=env)
        else:
            action, result = index_mcp._handle_tool_call(static[1], args)
    except (OSError, ValueError, TypeError):
        return None
    expected = index_mcp.format_mcp_response(action, result, revision=source)
    if "error" in result or not expected or len(expected) > 32768:
        return None
    # The current result includes the full file revision for source reads.
    raw_agent = payload.get("agent_id") or payload.get("subagent_id") or payload.get("agentId")
    identity = json.dumps([sid, raw_agent, turn, transcript, tool, args, expected], sort_keys=True)
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    path = state_home(env) / "codex-refetch" / f"{hashlib.sha256(sid.encode()).hexdigest()}.json"
    now = time.time()
    window = float(cfg.get("mcp_dedup_window_s", 120))
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        entries = {k: v for k, v in entries.items() if 0 <= now - v["ts"] <= window}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        entries = {}
    if completed:
        response = payload.get("tool_response")
        if not isinstance(response, dict) or tool_failed(payload):
            return None
        if response.get("content") != [{"type": "text", "text": expected}]:
            return None
        entries[key] = {"ts": now, "use_id": use_id, "denied": False}
        atomic_write_json(path, dict(sorted(entries.items(), key=lambda item: item[1]["ts"])[-64:]))
        return None
    previous = entries.get(key)
    if not previous or previous["denied"] or use_id == previous["use_id"]:
        return None
    if not _was_delivered(transcript, previous["use_id"], expected):
        return None
    reason = f"[Terser] Reuse the identical {action} result earlier in this turn, or retry."
    raw_bytes = len(expected.encode("utf-8"))
    reason_bytes = len(reason.encode("utf-8"))
    if raw_bytes <= reason_bytes:
        return None
    previous["denied"] = True
    atomic_write_json(path, entries)
    record_savings("mcp_dedup", raw_bytes, reason_bytes, host="codex", detail=tool, env=env)
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}


def remove_refetch_state(sid: str, env: dict[str, str]) -> None:
    path = state_home(env) / "codex-refetch" / f"{hashlib.sha256(sid.encode()).hexdigest()}.json"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
