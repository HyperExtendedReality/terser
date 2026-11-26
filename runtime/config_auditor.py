"""Terser — Static Context Bloat & Secret-Leak Auditor (`terse audit-config`).

Every token in always-on context files (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`,
`MEMORY.md`, MCP configs) is re-sent on EVERY turn. Removing 1,000 static tokens
saves 20,000–50,000 tokens over a 30-turn session.

Also checks for:
  - Overlapping legacy standalone token/index plugins running alongside `terser`.
  - `.claude/settings.local.json` secret exposure / missing `.gitignore` entry.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from runtime.config import estimate_tokens

_STATIC_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".cursorrules",
    ".claude/CLAUDE.md",
    ".agents/rules/AGENTS.md",
    "MEMORY.md",
    ".mcp.json",
)
_SECRET_RE = re.compile(
    r"(?:sk-[A-Za-z0-9\-]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|"
    r"AIza[0-9A-Za-z_\-]{30,}|Bearer\s+[A-Za-z0-9\-._~+/=]{20,}|"
    r"(?:postgres|mysql|mongodb|redis)://[^:\s/]+:[^@\s]+@)",
    re.I,
)


def audit_workspace(cwd: str, *, home: Path | None = None) -> dict[str, Any]:
    """Return structured static-context token audit & hygiene findings."""
    root = Path(cwd).resolve()
    home_dir = home or Path.home()
    files: list[dict[str, Any]] = []
    warnings: list[str] = []
    total_static_tokens = 0

    for rel in _STATIC_FILES:
        p = root / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tok = estimate_tokens(text)
        total_static_tokens += tok
        files.append({"path": rel, "tokens": tok, "lines": text.count("\n") + 1})
        if tok > 1500:
            warnings.append(
                f"`{rel}` is {tok:,} tokens (~{tok * 30:,} tokens over 30 turns). "
                "Trim verbose examples or move reference detail into on-demand skills."
            )

    # Check .claude/settings.local.json hygiene (secret-leak check)
    local_settings = root / ".claude" / "settings.local.json"
    if local_settings.is_file():
        try:
            raw = local_settings.read_text(encoding="utf-8", errors="replace")
            if _SECRET_RE.search(raw):
                warnings.append(
                    "SECURITY: `.claude/settings.local.json` matches a credential/token pattern! "
                    "Review and scrub secrets immediately."
                )
            gi = root / ".gitignore"
            gi_text = gi.read_text(encoding="utf-8", errors="replace") if gi.is_file() else ""
            if "settings.local.json" not in gi_text and ".claude/" not in gi_text:
                warnings.append(
                    "HYGIENE: `.claude/settings.local.json` exists but is not listed in `.gitignore`."
                )
        except OSError:
            pass

    # Check for overlapping legacy Antigravity plugins running alongside terser
    ag_plugins = home_dir / ".gemini" / "config" / "plugins"
    if ag_plugins.is_dir():
        legacy = [
            name for name in ("jcodemunch", "token-optimizer", "tokeneconomizer")
            if (ag_plugins / name).is_dir()
        ]
        if (ag_plugins / "terser").is_dir() and legacy:
            warnings.append(
                f"OVERLAP: Legacy plugin folder(s) present alongside `terser`: {', '.join(legacy)}. "
                "Ensure they are disabled in Antigravity Customizations to avoid duplicate hooks/schemas."
            )

    return {
        "workspace": str(root),
        "files": files,
        "total_static_tokens": total_static_tokens,
        "projected_30_turn_tokens": total_static_tokens * 30,
        "warnings": warnings,
    }


def format_audit(report: dict[str, Any]) -> str:
    lines = [
        f"Terser Static Context Audit — {report['workspace']}",
        f"  Always-on static files : {len(report['files'])}",
        f"  Per-turn static cost   : {report['total_static_tokens']:,} tokens",
        f"  30-turn projected cost : {report['projected_30_turn_tokens']:,} tokens",
    ]
    for f in report["files"]:
        lines.append(f"    - {f['path']:<28} {f['tokens']:>6,} tok ({f['lines']} lines)")
    if report["warnings"]:
        lines.append("  Findings:")
        for w in report["warnings"]:
            lines.append(f"    ! {w}")
    else:
        lines.append("  Findings: [PASS] No static bloat, plugin overlap, or secret-hygiene issues.")
    return "\n".join(lines)
