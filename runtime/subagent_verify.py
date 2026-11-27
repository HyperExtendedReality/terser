"""Terser — low-cost subagent routing with a zero-token double check.

Hooks cannot spawn subagents, so Terser does not run its own. The main model keeps deciding
when to delegate. When it spawns a read-only lookup subagent without choosing a model, Terser
may run that subagent on the low-cost tier, under two safeguards:

1. Contract (PreToolUse): the routed prompt gets a ``[Terser Verify]`` block. The subagent must
   cite ``path:line`` for every claim, keep backticked identifiers exact, and start its reply
   with ``ESCALATE:`` when the task needs more than lookups.
2. Double check (PostToolUse): a local, zero-token verifier checks the reply. Cited files must
   exist and cited lines must be in range. A backticked identifier on a citation line must
   appear near the cited line. Escalations, hedging, or a reply with no citations are flagged.
   On any problem the parent model gets a short note naming what failed and telling it to
   verify those claims or re-run the task on its own model.

Routing turns itself off for 7 days when, over that window, at least 8 routed subagents ran
and more than 35% of them failed verification. Stats live in ``~/.terser/routing.jsonl``.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from runtime.config import state_home

MARKER = "[Terser Verify]"
CONTRACT = (
    f"{MARKER} You run on a low-cost model; the parent agent double-checks your answer. "
    "Cite `path:line` for every factual claim and keep identifiers in backticks exactly as in the source. "
    "If you can't confirm something, say so; don't guess. If the task needs design judgment, edits, "
    "or reasoning over many files, reply with `ESCALATE: <reason>` on the first line and stop."
)

CHEAP_MODELS = {"claude": "haiku", "antigravity": "flash"}
AGY_SUBAGENT_MODELS = ("flash_lite", "flash", "pro")
CLAUDE_SUBAGENT_MODELS = ("haiku", "sonnet", "opus")
# Reasoning effort levels a routed subagent may be pinned to ("" = inherit).
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
AGY_EFFORT_LEVELS = ("low", "medium", "high", "max")
_EFFORT_GUIDANCE = {
    "low": "Effort=low: do a fast, single-pass targeted lookup and return concise cited facts.",
    "medium": "Effort=medium: cross-check definitions and callers across relevant files before answering.",
    "high": "Effort=high: verify edge cases and indirect references thoroughly before answering.",
    "xhigh": "Effort=xhigh: perform exhaustive verification across all relevant paths.",
    "max": "Effort=max: perform exhaustive verification across all relevant paths.",
}
# Read-only lookup agents only. A general-purpose agent can edit files, so it is routed only
# when its prompt shows no write intent.
_READONLY_TYPES = frozenset({"explore", "research"})
_ROUTABLE_TYPES = _READONLY_TYPES | {"general-purpose", ""}
_WRITE_INTENT_RE = re.compile(
    r"(?i)\b(?:implement\w*|fix(?:es|ed|ing)?|edit\w*|writ(?:e|es|ing)|rewrit\w*|chang(?:e|es|ing)|"
    r"refactor\w*|add(?:s|ing)?|creat(?:e|es|ing)|updat(?:e|es|ing)|modif\w*|delet\w*|remov\w*|"
    r"rename\w*|commit\w*|install\w*|migrat\w*|deploy\w*|patch\w*|generate\w*|build\w*)\b"
)
# Topics where a wrong answer is costly, whatever the classifier says.
_RISK_RE = re.compile(
    r"(?i)\b(?:secur\w*|auth\w*|password|secret|token|crypt\w*|payment|billing|permission\w*|"
    r"privacy|vulnerab\w*|exploit|concurren\w*|race|deadlock|thread\w*|migration|production|prod)\b"
)

_CITE_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?[\w./\\-]*[\w-]\.[A-Za-z0-9]{1,8}):(?P<line>\d{1,6})(?:[-–](?P<end>\d{1,6}))?"
)
_IDENT_RE = re.compile(r"`([A-Za-z_][\w.]{2,80})`")
_HEDGE_RE = re.compile(
    r"(?i)\b(?:i (?:could ?n[o']t|was unable to|can ?n[o']t) (?:find|locate|confirm|determine)|"
    r"i(?:'m| am) not (?:sure|certain)|i (?:assume|guess)d?)\b"
)
_ESCALATE_RE = re.compile(r"(?im)^\s*\**ESCALATE\**\s*:")

_BACKOFF_DAYS = 7
_BACKOFF_MIN_RUNS = 8
_BACKOFF_FAIL_RATIO = 0.35


def _log_path(env: dict[str, str] | None) -> Path:
    return state_home(env) / "routing.jsonl"


def _recent(env: dict[str, str] | None, now: float | None = None) -> list[dict[str, Any]]:
    path = _log_path(env)
    if not path.is_file():
        return []
    cutoff = (time.time() if now is None else now) - _BACKOFF_DAYS * 86400
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and float(rec.get("ts") or 0) >= cutoff:
                out.append(rec)
    except OSError:
        return []
    return out


def stats(env: dict[str, str] | None = None) -> dict[str, Any]:
    recs = _recent(env)
    routed = sum(1 for r in recs if r.get("event") == "routed")
    checked = [r for r in recs if r.get("event") == "verified"]
    failed = sum(1 for r in checked if not r.get("ok"))
    ratio = failed / len(checked) if checked else 0.0
    backed_off = len(checked) >= _BACKOFF_MIN_RUNS and ratio > _BACKOFF_FAIL_RATIO
    return {"routed": routed, "checked": len(checked), "failed": failed, "fail_ratio": round(ratio, 3), "backed_off": backed_off}


def _log(event: str, env: dict[str, str] | None, **fields: Any) -> None:
    try:
        path = _log_path(env)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": round(time.time(), 1), "event": event, **fields}) + "\n")
    except OSError:
        pass


def should_route(prompt: str, sub_type: str, requested_model: str, env: dict[str, str] | None = None) -> tuple[bool, str]:
    """``(route, reason)``. Never overrides a model the caller chose."""
    text = (prompt or "").strip()
    st = (sub_type or "").strip().lower()
    if not text or MARKER in text:
        return False, "empty or already routed"
    if (requested_model or "").strip().lower() not in ("", "inherit"):
        return False, "model chosen by caller"
    if st not in _ROUTABLE_TYPES:
        return False, f"subagent type `{st}` is not a lookup type"
    if st not in _READONLY_TYPES and _WRITE_INTENT_RE.search(text):
        return False, "prompt asks for changes"
    if _RISK_RE.search(text):
        return False, "high-risk topic"
    if len(text) > 1200:
        return False, "long multi-part brief"
    from runtime import task_router

    if task_router.classify_task(text).get("tier") != "low":
        return False, "not low complexity"
    if stats(env)["backed_off"]:
        return False, "auto backoff: recent routed answers failed verification"
    return True, "low-complexity lookup"


def routed_prompt(prompt: str, *, effort: str = "") -> str:
    eff = (effort or "").strip().lower()
    extra = _EFFORT_GUIDANCE.get(eff, "")
    block = f"{CONTRACT} {extra}".strip() if extra else CONTRACT
    return f"{prompt.rstrip()}\n\n{block}"


def record_routed(sub_type: str, model: str, env: dict[str, str] | None = None) -> None:
    _log("routed", env, type=sub_type or "general-purpose", model=model)


def response_text(resp: Any) -> str:
    """Plain text of an Agent/Task tool response (string, content blocks, or nested dict)."""
    parts: list[str] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, dict):
            for key in ("text", "content", "result", "output", "message"):
                if key in node:
                    walk(node[key], depth + 1)

    walk(resp)
    return "\n".join(p for p in parts if p.strip())


def _resolve(path_s: str, cwd: str) -> Path | None:
    raw = path_s.replace("\\", "/")
    cand = Path(raw)
    for p in ([cand] if cand.is_absolute() else [Path(cwd) / raw] if cwd else [cand]):
        try:
            if p.is_file():
                return p
        except OSError:
            return None
    return None


def verify(text: str, cwd: str, *, max_citations: int = 40) -> dict[str, Any]:
    """Deterministic double check of a routed subagent's reply. Zero model tokens."""
    problems: list[str] = []
    if _ESCALATE_RE.search(text):
        first = next((ln.strip() for ln in text.splitlines() if "ESCALATE" in ln), "ESCALATE")
        return {"ok": False, "escalated": True, "checked": 0, "problems": [first[:200]]}

    checked = 0
    cache: dict[str, list[str] | None] = {}
    for line in text.splitlines():
        cites = list(_CITE_RE.finditer(line))
        if not cites:
            continue
        idents = [
            ident for ident in _IDENT_RE.findall(line)
            if not ident.startswith("-") and "/" not in ident and "\\" not in ident
        ]
        line_windows: list[tuple[str, int, str, str]] = []
        for m in cites:
            if checked >= max_citations:
                break
            checked += 1
            path_s, ln = m.group("path"), int(m.group("line"))
            key = path_s
            if key not in cache:
                fp = _resolve(path_s, cwd)
                try:
                    cache[key] = fp.read_text(encoding="utf-8", errors="replace").splitlines() if fp else None
                except OSError:
                    cache[key] = None
            lines = cache[key]
            if lines is None:
                problems.append(f"`{path_s}` (cited at line {ln}) does not exist")
                continue
            if ln < 1 or ln > len(lines):
                problems.append(f"`{path_s}:{ln}` is past the end of the file ({len(lines)} lines)")
                continue
            window = "\n".join(lines[max(0, ln - 6): ln + 5])
            line_windows.append((path_s, ln, Path(path_s).stem, window))
        if line_windows:
            for ident in idents:
                leaf = ident.rsplit(".", 1)[-1]
                matched = any(
                    leaf in (stem, Path(ps).name) or leaf in win
                    for ps, _ln, stem, win in line_windows
                )
                if not matched:
                    ps0, ln0 = line_windows[0][0], line_windows[0][1]
                    problems.append(f"`{ident}` is not near `{ps0}:{ln0}`")
    if checked == 0 and len(text) > 200:
        problems.append("no `path:line` citations, so nothing could be checked")
    if _HEDGE_RE.search(text):
        problems.append("the reply hedges (e.g. 'not sure', 'probably')")
    return {"ok": not problems, "escalated": False, "checked": checked, "problems": problems[:6]}


def parent_note(result: dict[str, Any], model: str) -> str | None:
    """Short context note for the parent model, only when the double check found a problem."""
    if result.get("ok"):
        return None
    if result.get("escalated"):
        head = f"[Terser Verify] The low-cost ({model}) subagent escalated: {result['problems'][0]}"
    else:
        head = (
            f"[Terser Verify] The low-cost ({model}) subagent's answer failed the double check "
            f"({result.get('checked', 0)} citations checked): " + "; ".join(result.get("problems", []))
        )
    return head + ". Verify those claims yourself, or re-run the task without Terser routing by passing `model` explicitly."


def verify_reply_text(text: str, model: str, cwd: str, env: dict[str, str] | None = None) -> str | None:
    """Verify a routed subagent's plain-text reply, log stats, and return parent note on failure."""
    if not (text or "").strip():
        return None
    res = verify(text, cwd)
    _log("verified", env, ok=res["ok"], escalated=res["escalated"], checked=res["checked"], model=model or "low-cost")
    return parent_note(res, model or "low-cost")


def check_routed_result(tool_input: dict[str, Any], tool_resp: Any, cwd: str, env: dict[str, str] | None = None) -> str | None:
    """PostToolUse entry point: verify a routed subagent and return a parent note on failure."""
    prompt = str(tool_input.get("prompt") or tool_input.get("Prompt") or "")
    if MARKER not in prompt:
        return None
    model = str(tool_input.get("model") or tool_input.get("Model") or "low-cost")
    text = response_text(tool_resp)
    return verify_reply_text(text, model, cwd, env)


__all__ = [
    "AGY_EFFORT_LEVELS",
    "AGY_SUBAGENT_MODELS",
    "CHEAP_MODELS",
    "CLAUDE_SUBAGENT_MODELS",
    "CONTRACT",
    "EFFORT_LEVELS",
    "MARKER",
    "check_routed_result",
    "parent_note",
    "record_routed",
    "response_text",
    "routed_prompt",
    "should_route",
    "stats",
    "verify",
    "verify_reply_text",
]
