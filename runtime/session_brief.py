"""Terser — Unified Prompt-Cache-Safe SessionStart Brief.

Replaces separate SessionStart injections (continuity restore + repo
micro-skills + AST code-index status) with ONE strictly budget-capped
(<= 1,400 chars ≈ 350 tokens), deduplicated, cache-prefix-friendly block emitted
ONCE at session start.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from runtime.index_bridge import workspace_index_summary
from runtime.config import ensure_vendor_paths, normalize_workspace, workspace_hash

_LINE_MAX_CHARS = 180


def _clean_line(text: str) -> str:
    s = "".join(ch if ch == " " or ch.isprintable() else " " for ch in str(text or ""))
    return " ".join(s.split())[:_LINE_MAX_CHARS]


def _load_microskills(cwd: str, host: str, env: dict[str, str] | None = None) -> list[str]:
    """Read confirmed, cached Terser micro-skills for this repo (zero LLM calls)."""
    if not cwd:
        return []
    try:
        ensure_vendor_paths()
        from tokeneconomizer.plugin import paths as te_paths  # type: ignore

        e = env if env is not None else os.environ
        repo = te_paths.repo_identity(cwd, e)
        inj_path = Path(te_paths.repo_state_dir(repo, host, e)) / "injection.json"
        if not inj_path.is_file():
            return []
        data = json.loads(inj_path.read_text(encoding="utf-8"))
        rendered = str(data.get("rendered") or "").strip() if isinstance(data, dict) else ""
        if not rendered:
            return []
        lines: list[str] = []
        for raw in rendered.splitlines():
            ln = _clean_line(raw)
            if ln.startswith("- ") or ln.startswith("* "):
                lines.append("- " + ln[2:].strip())
        return lines[:6]
    except Exception:
        return []


_LEAN_LINE = "Replies: lead with the answer; show only changed code; skip restating tool output."


def _load_continuity(workspace: str, host: str, env: dict[str, str] | None = None) -> list[str]:
    """Load workspace-scoped Terser continuity context (strictly workspace-matched)."""
    if not workspace:
        return []
    env = env if env is not None else os.environ
    from runtime.analytics import continuity_path

    candidates: list[Path] = [continuity_path(workspace, env)]
    for path in candidates:
        try:
            if not path.is_file() or path.stat().st_size > 16384:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                continue
            lines = []
            for raw in text.splitlines():
                c = _clean_line(raw)
                if c and not c.startswith(("#", "Working dir:", "Captured:")):
                    lines.append(c)
            return lines[:6]
        except OSError:
            continue
    return []


def _line_key(line: str) -> str:
    return line.strip().removeprefix("- ").removeprefix("* ")


def _fit_lines(lines: list[str], room: int) -> list[str]:
    kept: list[str] = []
    for line in lines:
        cost = len(line) + bool(kept)
        if cost <= room:
            kept.append(line)
            room -= cost
    return kept


def build_session_brief(
    workspace: str,
    *,
    host: str,
    cfg: dict[str, Any],
    env: dict[str, str] | None = None,
    instruction_lines: tuple[str, ...] = (),
) -> tuple[str | None, str | None]:
    """Build ONE compact, budget-capped SessionStart brief.

    Returns ``(model_context_or_None, user_notice_or_None)``.
    """
    if not cfg.get("session_brief", True) or cfg.get("shadow"):
        return None, None

    max_chars = max(300, int(cfg.get("session_brief_max_chars", 1400)))
    sections: list[str] = []
    notice_parts: list[str] = []
    brief_header = "[Terser Brief]\n"
    facts: list[str] = []
    index_line = index_map = ""
    seen = {
        _line_key(line)
        for line in instruction_lines if _clean_line(line) == line.strip()
    }

    def new_lines(lines: list[str]) -> list[str]:
        unique: list[str] = []
        for line in lines:
            key = _line_key(line)
            if key and key not in seen:
                seen.add(key)
                unique.append(line)
        return unique

    # Emit static sections FIRST (Repo facts -> Code index / Repo map) and volatile
    # Prior session LAST so the longest possible prompt prefix stays KV-cache warm!

    # 1. Confirmed repo micro-skills (EnvFacts — static)
    if cfg.get("microskills", "envfacts") != "off":
        facts = _load_microskills(workspace, host, env)
        if facts and host != "codex":
            sections.append("Repo facts:\n" + "\n".join(facts))
            notice_parts.append(f"{len(facts)} repo fact(s)")

    # 2. Compact 1-line AST index status — OR byte-stable PageRank Repo Map if unindexed (static)
    idx = workspace_index_summary(workspace, env)
    if idx:
        surface = cfg.get("index_surface", "counter")
        hint = "use `order`/`route`" if surface == "counter" else "use `search_symbols`/`get_symbol_source`"
        # Prompt-cache stable: avoid volatile symbol counts in model context
        index_status = f"Code index: `{idx['repo']}` ready"
        index_line = index_status + "."
        if host != "codex":
            sections.append(f"{index_status} — {hint}.")
            notice_parts.append(f"code index ready ({idx['symbols']} symbols)")
    elif cfg.get("repo_map", True):
        try:
            from runtime import repo_map

            rmap = repo_map.build_repo_map(
                workspace,
                max_tokens=int(cfg.get("repo_map_max_tokens", 300)),
                env=env,
            )
            map_lines = {
                _line_key(line)
                for line in (rmap or "").splitlines() if line.strip()
            }
            if rmap and (host != "codex" or not map_lines.issubset(seen)):
                if host == "codex":
                    # Keep the optional map out of the way of exact resume details.
                    index_map = rmap
                else:
                    sections.append(rmap)
                    notice_parts.append("PageRank repo map")
        except Exception:
            pass

    # 3. Workspace-scoped continuity checkpoint (volatile — placed last for KV prefix stability)
    cont: list[str] = []
    if cfg.get("continuity_restore", True):
        cont = _load_continuity(workspace, host, env)
        if not cont:
            try:
                from runtime import context_sandbox

                cont = context_sandbox.build_continuity_snapshot(workspace, max_chars=450, env=env)
            except Exception:
                cont = []
        if cont and host != "codex":
            sections.append("Prior session:\n" + "\n".join(cont))
            notice_parts.append("continuity restored")

    if host == "codex":
        # Reserve room for complete continuity lines, then display static context first.
        budget = max_chars - len(brief_header)
        resume_heading = "Prior session:\n"
        observed = seen.copy()
        resume = _fit_lines(new_lines(cont), budget - len(resume_heading))
        seen = observed | {_line_key(line) for line in resume}
        continuity = resume_heading + "\n".join(resume) if resume else ""
        if resume:
            budget -= len(continuity) + 1

        fact_heading = "Repo facts:\n"
        new_facts = _fit_lines(new_lines(facts), budget - len(fact_heading))
        if new_facts:
            fact_section = fact_heading + "\n".join(new_facts)
            sections.append(fact_section)
            notice_parts.append(f"{len(new_facts)} repo fact(s)")
            budget -= len(fact_section) + 1
        index_section = index_line if idx else index_map
        if index_section and _line_key(index_section) not in observed and len(index_section) <= budget:
            sections.append(index_section)
            notice_parts.append(f"code index ready ({idx['symbols']} symbols)" if idx else "PageRank repo map")
        if resume:
            sections.append(continuity)
            notice_parts.append("continuity restored")

    if sections and host != "codex" and cfg.get("lean_replies", True):
        # Static, so it never breaks the prompt-cache prefix.
        sections.insert(0, _LEAN_LINE)
    if not sections:
        return None, None

    body = brief_header + "\n".join(sections)
    if len(body) > max_chars:
        body = body[: max_chars - 16].rstrip() + "\n... (capped)"

    user_notice = (
        f"Terser active: {', '.join(notice_parts)} (~{len(body) // 4} tok)."
        if notice_parts
        else None
    )
    return body, user_notice
