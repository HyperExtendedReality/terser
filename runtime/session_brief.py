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


def _load_microskills(cwd: str, env: dict[str, str] | None = None) -> list[str]:
    """Read confirmed, cached Terser micro-skills for this repo (zero LLM calls)."""
    if not cwd:
        return []
    try:
        ensure_vendor_paths()
        from tokeneconomizer.plugin import paths as te_paths  # type: ignore
        from tokeneconomizer.plugin import repos as te_repos  # type: ignore

        e = env if env is not None else os.environ
        root = te_paths.state_root(e)
        repo = te_repos.resolve(cwd, root=root)
        inj_path = Path(te_paths.repo_dir(root, repo.key)) / "injection.json"
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


def _load_continuity(workspace: str, host: str, env: dict[str, str] | None = None) -> list[str]:
    """Load workspace-scoped Terser continuity context (strictly workspace-matched)."""
    if not workspace:
        return []
    env = env if env is not None else os.environ
    ws_h = workspace_hash(workspace)
    norm_ws = normalize_workspace(workspace)

    candidates: list[Path] = []
    if host == "antigravity":
        ag_home = Path(os.path.expanduser(env.get("TOKEN_OPTIMIZER_ANTIGRAVITY_HOME") or "~/.gemini"))
        to_dir = ag_home / "token-optimizer"
    elif host == "codex":
        cx_home = Path(os.path.expanduser(env.get("CODEX_HOME") or "~/.codex"))
        to_dir = cx_home / "token-optimizer"
    else:
        cl_home = Path(os.path.expanduser(env.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
        to_dir = cl_home / "token-optimizer"

    if ws_h:
        candidates.append(to_dir / f"restore-context-{ws_h}.md")
    candidates.append(to_dir / "restore-context.md")

    for path in candidates:
        try:
            if not path.is_file() or path.stat().st_size > 16384:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                continue
            # For the unscoped global file, verify Working dir matches workspace
            if path.name == "restore-context.md":
                matched = False
                for ln in text.splitlines():
                    s = ln.strip()
                    if s.lower().startswith("- working dir:"):
                        if normalize_workspace(s.split(":", 1)[1]) == norm_ws:
                            matched = True
                        break
                if not matched:
                    continue
            lines = []
            for raw in text.splitlines():
                c = _clean_line(raw)
                if c and not c.startswith("#"):
                    lines.append(c)
            return lines[:6]
        except OSError:
            continue
    return []


def build_session_brief(
    workspace: str,
    *,
    host: str,
    cfg: dict[str, Any],
    env: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Build ONE compact, budget-capped SessionStart brief.

    Returns ``(model_context_or_None, user_notice_or_None)``.
    """
    if not cfg.get("session_brief", True) or cfg.get("shadow"):
        return None, None

    max_chars = max(300, int(cfg.get("session_brief_max_chars", 1400)))
    sections: list[str] = []
    notice_parts: list[str] = []

    # Emit static sections FIRST (Repo facts -> Code index / Repo map) and volatile
    # Prior session LAST so the longest possible prompt prefix stays KV-cache warm!

    # 1. Confirmed repo micro-skills (EnvFacts — static)
    if cfg.get("microskills", "envfacts") != "off":
        facts = _load_microskills(workspace, env)
        if facts:
            sections.append("Repo facts:\n" + "\n".join(facts))
            notice_parts.append(f"{len(facts)} repo fact(s)")

    # 2. Compact 1-line AST index status — OR byte-stable PageRank Repo Map if unindexed (static)
    idx = workspace_index_summary(workspace, env)
    if idx:
        surface = cfg.get("index_surface", "counter")
        hint = "use `order`/`route`" if surface == "counter" else "use `search_symbols`/`get_symbol_source`"
        # Prompt-cache stable: avoid volatile symbol counts in model context
        sections.append(f"Code index: `{idx['repo']}` ready — {hint}.")
        notice_parts.append(f"code index ready ({idx['symbols']} symbols)")
    elif cfg.get("repo_map", True):
        try:
            from runtime import repo_map

            rmap = repo_map.build_repo_map(
                workspace,
                max_tokens=int(cfg.get("repo_map_max_tokens", 300)),
                env=env,
            )
            if rmap:
                sections.append(rmap)
                notice_parts.append("PageRank repo map")
        except Exception:
            pass

    # 3. Workspace-scoped continuity checkpoint (volatile — placed last for KV prefix stability)
    if cfg.get("continuity_restore", True):
        cont = _load_continuity(workspace, host, env)
        if not cont:
            try:
                from runtime import context_sandbox

                cont = context_sandbox.build_continuity_snapshot(workspace, max_chars=450, env=env)
            except Exception:
                cont = []
        if cont:
            sections.append("Prior session:\n" + "\n".join(cont))
            notice_parts.append("continuity restored")

    if not sections:
        return None, None

    body = "[Terser Brief]\n" + "\n".join(sections)
    if len(body) > max_chars:
        body = body[: max_chars - 16].rstrip() + "\n... (capped)"

    user_notice = (
        f"Terser active: {', '.join(notice_parts)} (~{len(body) // 4} tok)."
        if notice_parts
        else None
    )
    return body, user_notice
