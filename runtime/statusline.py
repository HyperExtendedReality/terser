#!/usr/bin/env python3
"""Terser status line for Claude Code (``statusLine`` setting, also shown in the desktop app).

Reads the status-line JSON Claude Code sends on stdin and prints one line, e.g.
``Terser ▸ saved 48.2k this session · ctx 41%``. Cheap by design: it reads only the
session flag file, the tail of ``savings.jsonl`` and the tail of the transcript.

Enable with ``python scripts/install.py --statusline`` or by hand in
``~/.claude/settings.json``::

    "statusLine": {"type": "command", "command": "python <plugin>/runtime/statusline.py"}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
if str(_THIS.parents[1]) not in sys.path:
    sys.path.insert(0, str(_THIS.parents[1]))

from runtime.config import state_home  # noqa: E402


def _fmt(n: int) -> str:
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


def _started_at(session_id: str, env: dict[str, str]) -> float | None:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:80]
    p = state_home(env) / "session-flags" / f"{safe}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        v = data.get("started_at") if isinstance(data, dict) else None
        return float(v) if v else None
    except (OSError, ValueError, TypeError):
        return None


def render(payload: dict[str, Any], env: dict[str, str] | None = None) -> str:
    env = env if env is not None else dict(os.environ)
    from runtime import autopilot

    parts = ["Terser"]
    sid = str(payload.get("session_id") or "")
    started = _started_at(sid, env) if sid else None
    if started:
        parts.append(f"saved {_fmt(autopilot.session_saved_tokens(started, env))} this session")
    tp = str(payload.get("transcript_path") or "")
    if tp:
        fill = autopilot.context_fill(tp, "claude")
        if fill is not None:
            parts.append(f"ctx {fill:.0%}")
    if len(parts) == 1:
        parts.append("active")
    return parts[0] + " ▸ " + " · ".join(parts[1:])


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    try:
        line = render(payload if isinstance(payload, dict) else {})
    except Exception:
        line = "Terser"
    sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
