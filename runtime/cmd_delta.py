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
import os
import re
import time
from pathlib import Path
from typing import Any

from runtime import context_sandbox
from runtime.config import atomic_write_json, record_savings, state_home

_TTL_SECONDS = 1800.0  # 30-minute delta window
_MAX_ENTRIES = 40
_MAX_STORED_CHARS = 32768
_MAX_DIFF_LINES = 45
_MAX_DIFF_RATIO = 0.45
_MIN_CHARS_FOR_DELTA = 300

# Commands whose repeated runs benefit hugely from delta-diffing
_DELTA_ELIGIBLE_RE = re.compile(
    r"^\s*(?:(?:python3?\s+-m|uv\s+run)\s+)?(?:pytest|unittest|jest|vitest|mocha|"
    r"(?:npm|pnpm|yarn)\s+(?:test|lint|build|check|run\s+(?:test|lint|build|check|typecheck))|"
    r"cargo\s+(?:test|check|build|clippy)|go\s+test|"
    r"dotnet\s+(?:test|build)|(?:gradle|gradlew|mvn|mvnw)\s+(?:test|build|check)|"
    r"gh\s+(?:pr\s+checks|run\s+view)|(?:terraform|tofu)\s+plan|kubectl\s+get|docker\s+ps|"
    r"npx\s+(?:tsc|eslint|biome|oxlint)|tsc|mypy|pyright|ruff|eslint|biome|oxlint|flake8|git\s+(?:status|diff|log))\b",
    re.I,
)


def _store_path(session_id: str, env: dict[str, str] | None = None) -> Path:
    safe = session_id or "default"
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}", safe):
        safe = hashlib.sha256(safe.encode("utf-8")).hexdigest()
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


def _cmd_key(
    command: str | tuple[str, ...], *, cwd: str | None = None, env: dict[str, str] | None = None,
    host: str = "unknown", session_id: str = "",
) -> str:
    # Hash the execution context without writing environment values or arguments to disk.
    identity = json.dumps((host, session_id, command, cwd or os.getcwd(), sorted((os.environ if env is None else env).items())))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


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
    cwd: str | None = None,
    argv: tuple[str, ...] | None = None,
) -> tuple[str, bool]:
    """Compare `compressed_output` against the previous run of `command` in `session_id`.

    Returns ``(output_to_serve, was_delta_applied)``.
    """
    if not session_id or not command or len(compressed_output) < _MIN_CHARS_FOR_DELTA:
        return compressed_output, False
    if not _DELTA_ELIGIBLE_RE.match(command):
        return compressed_output, False
    strict = host == "codex"
    if len(compressed_output) > _MAX_STORED_CHARS or (strict and returncode != 0):
        return compressed_output, False

    now = time.time() if now is None else float(now)
    key = _cmd_key(argv if argv is not None else command, cwd=cwd, env=env, host=host, session_id=session_id)
    store = _load(session_id, env)
    archive_id = context_sandbox.store_output(compressed_output, command=command, env=env, require_exact=True)
    if not archive_id:
        return compressed_output, False
    prev = store.get(key)

    cur_sha = hashlib.sha256(compressed_output.encode("utf-8", errors="replace")).hexdigest()
    cur_norm_sha = cur_sha if strict else hashlib.sha256(
        _normalize_for_delta_hash(compressed_output).encode("utf-8", errors="replace")
    ).hexdigest()
    cur_lines = compressed_output.splitlines(keepends=True)

    result_text = compressed_output
    applied = False
    replay = context_sandbox.retrieval_hint(archive_id, "raw", env=env).strip("`")
    replay_label = "full stdout" if strict else "stored output"

    if isinstance(prev, dict) and 0 <= now - float(prev.get("ts") or 0.0) <= _TTL_SECONDS:
        prev_sha = prev.get("sha")
        prev_norm_sha = prev.get("norm_sha") or prev_sha
        prev_rc = prev.get("rc")
        baseline = str(prev.get("archive_id") or "")
        prev_text = context_sandbox.read_stored_output(baseline, env=env)
        if prev_text and hashlib.sha256(prev_text.encode("utf-8", errors="replace")).hexdigest() != prev_sha:
            prev_text = None

        # Case 1: Byte-identical (or timing-only-varying) output and same exit code
        if prev_text and (prev_sha == cur_sha or prev_norm_sha == cur_norm_sha) and prev_rc == returncode:
            tail = _last_nonempty_line(compressed_output)
            tail_part = f" Last line: `{tail}`" if tail else ""
            candidate = (
                f"[Terser Delta] Identical output to previous run (exit={returncode}, {len(cur_lines)} lines). "
                f"Baseline `{baseline}`; {replay_label} `{replay}`.{tail_part}"
            )
            if len(candidate) < len(compressed_output):
                result_text = candidate
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
                diff_body = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff_lines)
                tail = _last_nonempty_line(compressed_output)
                header = f"[Terser Delta vs baseline `{baseline}`, exit {prev_rc}->{returncode}. {replay_label} `{replay}`.]"
                candidate = f"{header}\n```diff\n{diff_body}```"
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
        "archive_id": archive_id,
    }
    _save(session_id, store, env)

    if applied:
        record_savings(
            "cmd_delta",
            len(compressed_output),
            len(result_text),
            host=host,
            detail=command.split(maxsplit=1)[0],
            env=env,
        )
    return result_text, applied
