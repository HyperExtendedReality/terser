"""Terser — Zero-Cost Task Complexity Router (`terse route "<task>"`).

Classifies a coding task in <1ms (zero API calls) into low / medium / high
complexity and recommends the most cost-effective model tier across Antigravity 2.0,
Claude Code, and Codex—saving 50-85% on routine tasks while reserving frontier
models for multi-file architecture, concurrency, security, and deep debugging.
"""
from __future__ import annotations

import re
from typing import Any

_HIGH_SIGNALS = (
    (re.compile(r"(?i)\b(?:architect|redesign|migrate|refactor\s+across|multi-?file|distributed|concurren|race\s+condition|deadlock|memory\s+leak|security\s+audit|cryptograph|zero-?day|kernel|compiler|optimizer)\b"), 35),
    (re.compile(r"(?i)\b(?:root\s+cause|intermittent|flaky|heisenbug|performance\s+regression|profiling|benchmark|algorithm)\b"), 25),
    (re.compile(r"(?i)\b(?:design\s+a|from\s+scratch|end-?to-?end|system\s+design|combine\s+.*plugins|state\s+machine)\b"), 20),
)
_LOW_SIGNALS = (
    (re.compile(r"(?i)\b(?:typo|rename|format|lint|comment|docstring|bump\s+version|add\s+import|fix\s+warning|changelog|readme|gitignore|boilerplate|simple\s+test|unit\s+test\s+for)\b"), -25),
    (re.compile(r"(?i)\b(?:explain|summarize|what\s+does|where\s+is|find\s+where|list\s+all|show\s+me)\b"), -15),
)

_TIERS: dict[str, dict[str, str]] = {
    "low": {
        "antigravity": "Flash Lite / Flash (subagent: flash_lite or flash, effort: low)",
        "claude": "Claude Haiku (fast/cheap, ~80% cheaper than Opus)",
        "codex": "GPT-5.4-Mini / Codex Mini (Low reasoning effort)",
    },
    "medium": {
        "antigravity": "Gemini Flash / Gemini 4 Neon (subagent: flash, effort: medium)",
        "claude": "Claude Sonnet (balanced, ~60% cheaper than Opus)",
        "codex": "GPT-5.4 / Codex (Medium reasoning effort)",
    },
    "high": {
        "antigravity": "Gemini Pro / Gemini 4 Neon (subagent: pro, effort: high)",
        "claude": "Claude Opus (frontier reasoning)",
        "codex": "GPT-5.4 Pro / Codex High (High reasoning effort)",
    },
}


def classify_task(prompt: str) -> dict[str, Any]:
    text = (prompt or "").strip()
    score = 40  # neutral baseline (medium)
    reasons: list[str] = []

    if len(text) > 500:
        score += 15
        reasons.append("long multi-part spec")
    elif len(text) < 80:
        score -= 10
        reasons.append("short targeted prompt")

    for pat, delta in _HIGH_SIGNALS:
        m = pat.search(text)
        if m:
            score += delta
            reasons.append(f"high-complexity signal `{m.group(0)}`")
    for pat, delta in _LOW_SIGNALS:
        m = pat.search(text)
        if m:
            score += delta
            reasons.append(f"routine signal `{m.group(0)}`")

    score = max(0, min(100, score))
    tier = "low" if score < 30 else ("high" if score >= 65 else "medium")
    return {
        "tier": tier,
        "score": score,
        "reasons": reasons or ["standard coding task"],
        "recommendations": _TIERS[tier],
    }


def format_route(res: dict[str, Any]) -> str:
    rec = res["recommendations"]
    return "\n".join([
        f"Terser Task Router -> Tier: {res['tier'].upper()} (complexity {res['score']}/100)",
        f"  Signals     : {', '.join(res['reasons'])}",
        f"  Antigravity : {rec['antigravity']}",
        f"  Claude Code : {rec['claude']}",
        f"  Codex       : {rec['codex']}",
    ])
