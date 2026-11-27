"""Terser — Zero-Command Autopilot.

Everything here exists so users never need a slash command for Terser to do its job:

  * ``quick_health``      SessionStart: stat-only self-check; user-only warning on failure.
  * ``mcp_failure_hint``  PostToolUseFailure on Terser's MCP: exact doctor command to run.
  * ``intent_hint``       UserPromptSubmit: plain-language Terser questions -> exact tool call.
  * ``context_fill``      UserPromptSubmit: Claude/Codex live context-fill from the transcript.
  * ``session_summary``   Stop: milestone-throttled, user-only savings line.

All functions are fail-open (return ``None`` on any error) and cost zero model tokens unless
they have something specific to say.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from runtime.config import TE_VENDOR_DIR, state_home

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_TAIL_BYTES = 256 * 1024


def cli_command(host: str) -> str:
    """Absolute, quoted Terser CLI invocation usable from the agent's shell tool."""
    scripts = _PLUGIN_ROOT / "scripts"
    # Claude Code's Bash tool is POSIX sh even on Windows; Codex/Antigravity use the native shell.
    if host == "claude" or os.name != "nt":
        return f'"{(scripts / "terse").as_posix()}" --host {host}'
    # PowerShell needs the call operator before a quoted executable path.
    return f'& "{scripts / "terse.cmd"}" --host {host}'


def shell_tool(host: str) -> str:
    """Name of the agent tool that can run ``cli_command`` (the sh launcher needs Bash)."""
    return "the Bash tool" if host == "claude" else "the shell tool"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def quick_health(env: dict[str, str] | None = None) -> list[str]:
    """Stat-only (<1ms) checks for failures that silently disable Terser features."""
    problems: list[str] = []
    if sys.version_info < (3, 9):
        problems.append(f"Python {sys.version.split()[0]} is too old (need 3.9+)")
    if not (_PLUGIN_ROOT / "runtime" / "index_mcp.py").is_file():
        problems.append("AST index MCP server is missing")
    if not TE_VENDOR_DIR.is_dir():
        problems.append("micro-skills engine is missing")
    try:
        home = state_home(env)
        home.mkdir(parents=True, exist_ok=True)
        probe = home / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        problems.append("state directory is not writable")
    return problems


def health_notice(host: str, env: dict[str, str] | None = None) -> str | None:
    problems = quick_health(env)
    if not problems:
        return None
    return (
        f"Terser degraded: {'; '.join(problems)}. "
        f"Diagnose with: {cli_command(host)} doctor"
    )


def mcp_failure_hint(tool_name: str, host: str) -> str | None:
    name = (tool_name or "").lower()
    if not name.startswith("mcp__") or "terser" not in name:
        return None
    return (
        "[Terser] The Terser index MCP call failed. Fall back to Read/Grep for this step; "
        f"to diagnose, run `{cli_command(host)} doctor` via {shell_tool(host)}."
    )


# ---------------------------------------------------------------------------
# Plain-language intents
# ---------------------------------------------------------------------------

# "terse" alone is an ordinary adjective ("keep replies terse"), so only the product name counts.
_TERSER_RE = re.compile(r"(?i)\bterser\b")
# Host-generated turns (task notifications, reminders, command echoes) are not user requests.
_MACHINE_PROMPT_RE = re.compile(
    r"(?i)<task-notification>|<system-reminder>|\[SYSTEM NOTIFICATION|<side-sessions-event>"
    r"|<ci-monitor-event>|<command-(?:name|message|args)>|<local-command-stdout>|<bash-(?:input|stdout)>"
)
# Requests to work ON Terser (or any code) are development tasks, not questions about Terser's state.
_DEV_WORK_RE = re.compile(
    r"(?i)\b(?:implement\w*|refactor\w*|improv\w*|fix(?:es|ed|ing)?|bugs?|add(?:s|ed|ing)?|rewrit\w*|"
    r"edit(?:s|ed|ing)?|modify\w*|feature\w*|tests?|function|module|commit|source|code)\b"
)
_INTENT_MAX_CHARS = 300  # longer prompts are task specs; a hint there is noise
_INTENT_WINDOW = 60  # intent word must sit this close to "terser"
_INTENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(?:broken|working|failing|errors?|doctor|health|diagnos\w*|installed)\b"), "doctor"),
    (re.compile(r"(?i)\b(?:dashboard|chart|graph|analytics)\b"), "dashboard"),
    (re.compile(r"(?i)\b(?:audit|waste|wasting|bloat)\b"), "audit"),
    (re.compile(r"(?i)\b(?:disable|enable|turn\s+(?:on|off)|toggle|configure|config|settings?)\b"), "config"),
    (re.compile(r"(?i)\b(?:sav(?:e|ed|ing|ings)|status|stats|tokens?|active|doing)\b"), "status"),
)
_ARCHIVE_RE = re.compile(
    r"(?i)\b(?:full|whole|complete|rest\s+of\s+the|entire)\s+(?:output|log|results?)\b"
    r"|\b(?:omitted|truncated|elided)\s+(?:lines|output)\b"
)


def intent_hint(prompt: str, host: str) -> str | None:
    """Map a plain-language Terser request to the exact call the agent should make."""
    text = (prompt or "").strip()
    if not text or len(text) > _INTENT_MAX_CHARS or text.startswith("/"):
        return None
    if _MACHINE_PROMPT_RE.search(text):
        return None
    if _ARCHIVE_RE.search(text):
        return (
            "[Terser] Archived full outputs are retrievable without re-running: "
            '`order(action="search_sandbox", args={"archive_id": "--last", "query": "<terms>"})` or '
            '`order(action="expand_sandbox", args={"archive_id": "--last", "start_line": 1, "end_line": 100})`.'
        )
    names = [m.span() for m in _TERSER_RE.finditer(text)]
    if not names or _DEV_WORK_RE.search(text):
        return None
    cli = cli_command(host)
    for pat, intent in _INTENTS:
        near = any(
            abs(m.start() - n_start) <= _INTENT_WINDOW or abs(m.end() - n_end) <= _INTENT_WINDOW
            for m in pat.finditer(text)
            for n_start, n_end in names
        )
        if not near:
            continue
        if intent == "config":
            return (
                f"[Terser] Settings: `{cli} config show`; change with `{cli} config set <key>=<value>` "
                "(e.g. bash_compress, outline_first, read_cache, loop_guard, index_auto_init, shadow). "
                f"Run via {shell_tool(host)}."
            )
        return f"[Terser] Run `{cli} {intent}` via {shell_tool(host)} and summarize the result."
    return None


# ---------------------------------------------------------------------------
# Live context fill (Claude Code & Codex transcripts)
# ---------------------------------------------------------------------------

def _tail_lines(path: Path, max_bytes: int = _TAIL_BYTES) -> list[str]:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > max_bytes else lines


def _claude_window(model: str) -> int:
    m = (model or "").lower()
    return 1_000_000 if "[1m]" in m else 200_000


def context_fill(transcript_path: str | None, host: str) -> float | None:
    """Return the latest context fill fraction (0..1) from a Claude/Codex/Antigravity session."""
    if not transcript_path:
        return None
    p = Path(transcript_path)
    if (host or "").strip().lower() == "antigravity" or (
        p.name in ("transcript.jsonl", "transcript_full.jsonl") and p.parent.name == "logs"
    ):
        try:
            if len(p.parents) >= 3 and p.parent.parent.name == ".system_generated":
                from runtime import antigravity_proto

                proto_fill = antigravity_proto.live_context_fill(p.parents[2].name, str(p))
                if isinstance(proto_fill, float):
                    return proto_fill
        except Exception:
            pass
    try:
        if not p.is_file():
            return None
        lines = _tail_lines(p)
    except OSError:
        return None
    for line in reversed(lines):
        if '"usage"' not in line and '"token_count"' not in line and '"PLANNER_RESPONSE"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        # Antigravity 2.0 transcript: {"type":"PLANNER_RESPONSE","model":..,"input_tokens":..,"cache_read_tokens":..}
        if rec.get("type") == "PLANNER_RESPONSE":
            used = sum(int(rec.get(k) or 0) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
            if used > 0:
                m = str(rec.get("model") or "").lower()
                window = 1_000_000 if ("gemini" in m or "[1m]" in m or used > 200_000) else 200_000
                return min(1.0, used / window)
            continue
        # Codex rollout: {"type":"event_msg","payload":{"type":"token_count","info":{...}}}
        payload = rec.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "token_count":
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            last = info.get("last_token_usage") or {}
            window = int(info.get("model_context_window") or 0)
            used = int(last.get("input_tokens") or 0)
            if window > 0 and used > 0:
                return min(1.0, used / window)
            continue
        # Claude Code transcript: {"type":"assistant","message":{"model":..,"usage":{..}}}
        msg = rec.get("message")
        if not isinstance(msg, dict) or rec.get("isSidechain"):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        used = sum(
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        if used > 0:
            return min(1.0, used / _claude_window(str(msg.get("model") or "")))
    return None


FILL_70 = (
    "[Terser] Context >=70% full. Prefer symbol lookups, scoped CLI output, "
    "and concise replies without repeating unchanged code."
)
FILL_85 = (
    "[Terser] Context >=85% full. Keep edits focused and replies concise; "
    "finish the current step before starting broad exploration."
)


# ---------------------------------------------------------------------------
# Session savings summary
# ---------------------------------------------------------------------------

def session_saved_tokens(since_ts: float, env: dict[str, str] | None = None) -> int:
    log = state_home(env) / "savings.jsonl"
    if not log.is_file():
        return 0
    try:
        lines = _tail_lines(log, max_bytes=1024 * 1024)
    except OSError:
        return 0
    total = 0
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if float(rec.get("ts") or 0.0) < since_ts:
            break
        total += int(rec.get("saved_tok") or 0)
    return total


def next_milestone(saved: int, last_shown: int, *, floor: int = 5000) -> bool:
    """Show at >= floor, then only after savings at least double (a few lines per session max)."""
    if saved < floor:
        return False
    return last_shown <= 0 or saved >= 2 * last_shown


def summary_line(saved: int) -> str:
    return f"Terser saved ~{saved:,} tokens this session."


__all__ = [
    "FILL_70",
    "FILL_85",
    "cli_command",
    "context_fill",
    "health_notice",
    "intent_hint",
    "mcp_failure_hint",
    "next_milestone",
    "quick_health",
    "session_saved_tokens",
    "summary_line",
]
