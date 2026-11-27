"""Terser — Cross-Run Command Output Delta-Diff (Next-Action Preserving).

During iterative fix/test loops, agents run the same test, lint, typecheck, or
git command 5-15 times in a row. Instead of re-billing the entire output every
time:
  - Identical output + identical exit code -> 1-line receipt (~99% saved).
  - Small change (<= 45 diff lines and < 45% of output) -> exact unified diff
    plus the final summary line (~75-92% saved).
  - Large change or first run -> full compressed output (guardrail: never
    withholds signal that could cause an extra re-run turn).

Pure Python stdlib, per-session state under ~/.terser/cmd-delta/.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from runtime.config import atomic_write_json, record_savings, state_home

_TTL_SECONDS = 1800.0  # 30-minute delta window
_MAX_ENTRIES = 40
_MAX_STORED_CHARS = 32768
_MAX_DIFF_LINES = 45
_MAX_DIFF_RATIO = 0.45
_MIN_CHARS_FOR_DELTA = 300

# Commands whose repeated runs benefit hugely from delta-diffing
_DELTA_ELIGIBLE_RE = re.compile(
    r"^\s*(?:python3?\s+-m\s+)?(?:pytest|unittest|jest|vitest|mocha|npm\s+test|"
    r"pnpm\s+test|yarn\s+test|cargo\s+(?:test|check|build|clippy)|go\s+test|"
    r"npx\s+tsc|tsc|mypy|pyright|ruff|eslint|flake8|git\s+(?:status|diff|log))\b",
    re.I,
)


def _store_path(session_id: str, env: dict[str, str] | None = None) -> Path:
    safe = re.sub(r"[^0-9a-zA-Z_.-]", "_", session_id or "default")[:80]
    return state_home(env) / "cmd-delta" / f"{safe}.json"


def clear_session_state(session_id: str, env: dict[str, str] | None = None) -> None:
    if not session_id:
        return
    try:
        _store_path(session_id, env).unlink(missing_ok=True)
    except OSError:
        pass


def _load(session_id: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    p = _store_path(session_id, env)
    try:
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _save(session_id: str, data: dict[str, Any], env: dict[str, str] | None = None) -> None:
    if len(data) > _MAX_ENTRIES:
        ordered = sorted(
            data.items(),
            key=lambda kv: kv[1].get("ts", 0) if isinstance(kv[1], dict) else 0,
            reverse=True,
        )[:_MAX_ENTRIES]
        data = dict(ordered)
    atomic_write_json(_store_path(session_id, env), data)


def _cmd_key(command: str) -> str:
    norm = " ".join((command or "").split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:20]


_VOLATILE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b"
    r"|\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"
    r"|\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|µs|us|ns)\b"
    r"|\bpid[=:\s]+\d+\b",
    re.IGNORECASE,
)


def _normalize_for_delta_hash(text: str) -> str:
    """Normalize volatile durations/timestamps/PIDs for identical-outcome detection only."""
    return _VOLATILE_RE.sub("<T>", text)


def _last_nonempty_line(text: str) -> str:
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s:
            return s[:160]
    return ""


def apply_command_delta(
    session_id: str,
    command: str,
    compressed_output: str,
    *,
    returncode: int = 0,
    host: str = "unknown",
    now: float | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, bool]:
    """Compare `compressed_output` against the previous run of `command` in `session_id`.

    Returns ``(output_to_serve, was_delta_applied)``.
    """
    if not session_id or not command or len(compressed_output) < _MIN_CHARS_FOR_DELTA:
        return compressed_output, False
    if not _DELTA_ELIGIBLE_RE.match(command):
        return compressed_output, False

    now = time.time() if now is None else float(now)
    key = _cmd_key(command)
    store = _load(session_id, env)
    prev = store.get(key)

    cur_sha = hashlib.sha256(compressed_output.encode("utf-8", errors="replace")).hexdigest()
    cur_norm_sha = hashlib.sha256(
        _normalize_for_delta_hash(compressed_output).encode("utf-8", errors="replace")
    ).hexdigest()
    cur_lines = compressed_output.splitlines(keepends=True)

    result_text = compressed_output
    applied = False

    if isinstance(prev, dict) and (now - float(prev.get("ts") or 0.0)) <= _TTL_SECONDS:
        prev_sha = prev.get("sha")
        prev_norm_sha = prev.get("norm_sha") or prev_sha
        prev_rc = prev.get("rc")
        prev_text = prev.get("text")
        elapsed = max(1, int(now - float(prev.get("ts") or now)))

        # Case 1: Byte-identical (or timing-only-varying) output and same exit code
        if (prev_sha == cur_sha or prev_norm_sha == cur_norm_sha) and prev_rc == returncode:
            tail = _last_nonempty_line(compressed_output)
            tail_part = f" Last line: `{tail}`" if tail else ""
            result_text = (
                f"[Terser Delta] Identical output to previous `{command[:50]}` run "
                f"({elapsed}s ago, exit={returncode}, {len(cur_lines)} lines).{tail_part}"
            )
            applied = True
        # Case 2: Small diff against previous run (NAP-safe: keeps exit transition + summary)
        elif isinstance(prev_text, str) and prev_text:
            prev_lines = prev_text.splitlines(keepends=True)
            diff_lines = list(
                difflib.unified_diff(
                    prev_lines,
                    cur_lines,
                    fromfile="prev_run",
                    tofile="this_run",
                    n=1,
                )
            )
            if (
                diff_lines
                and len(diff_lines) <= _MAX_DIFF_LINES
                and len(cur_lines) >= 10
                and (len(diff_lines) / max(len(cur_lines), 1)) <= _MAX_DIFF_RATIO
            ):
                diff_body = "".join(diff_lines).rstrip()
                tail = _last_nonempty_line(compressed_output)
                header = f"[Terser Delta vs prior run ({elapsed}s ago, exit {prev_rc}->{returncode})]"
                candidate = f"{header}\n```diff\n{diff_body}\n```"
                if tail and tail not in diff_body:
                    candidate += f"\nSummary: {tail}"
                if len(candidate) < int(len(compressed_output) * 0.75):
                    result_text = candidate
                    applied = True

    # Always store latest snapshot so the NEXT run diffs against THIS run
    store[key] = {
        "sha": cur_sha,
        "norm_sha": cur_norm_sha,
        "rc": returncode,
        "ts": now,
        "text": compressed_output[:_MAX_STORED_CHARS],
    }
    _save(session_id, store, env)

    if applied:
        record_savings(
            "cmd_delta",
            len(compressed_output),
            len(result_text),
            host=host,
            detail=command[:60],
            env=env,
        )
    return result_text, applied
