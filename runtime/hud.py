#!/usr/bin/env python3
"""Terser HUD feed: one JSON snapshot for the Claude desktop status band and details pane.

``terse hud --json [--transcript PATH] [--session ID]`` prints::

    {"version", "enabled", "shadow", "session": {...} | null, "lifetime": {...},
     "price": {...}, "index": {...} | null, "features": {...}, "funnel": [...]}

Cheap enough to run once per turn: it reads ``savings.jsonl``, the session flag file
and (when given) one transcript.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_THIS = Path(__file__).resolve()
if str(_THIS.parents[1]) not in sys.path:
    sys.path.insert(0, str(_THIS.parents[1]))

from runtime import __version__  # noqa: E402
from runtime.config import load_config, load_funnel_summary, state_home  # noqa: E402


def _safe_id(session_id: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:80]


def _started_at(session_id: str) -> float | None:
    p = state_home() / "session-flags" / f"{_safe_id(session_id)}.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        v = data.get("started_at") if isinstance(data, dict) else None
        return float(v) if v else None
    except (OSError, ValueError, TypeError):
        return None


def _savings(since: float = 0.0) -> tuple[int, int, int, dict[str, dict[str, int]]]:
    """(events, raw_tok, saved_tok, by_cat) for savings.jsonl rows at or after ``since``."""
    log = state_home() / "savings.jsonl"
    by_cat: dict[str, dict[str, int]] = {}
    events = raw = saved = 0
    if not log.is_file():
        return 0, 0, 0, {}
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0, 0, 0, {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or float(rec.get("ts") or 0) < since:
            continue
        cat = str(rec.get("cat") or "other")
        s, r = int(rec.get("saved_tok") or 0), int(rec.get("raw_tok") or 0)
        c = by_cat.setdefault(cat, {"events": 0, "raw_tok": 0, "saved_tok": 0})
        c["events"] += 1
        c["raw_tok"] += r
        c["saved_tok"] += s
        events += 1
        raw += r
        saved += s
    return events, raw, saved, by_cat


def _cats(by_cat: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    out = []
    for cat, c in sorted(by_cat.items(), key=lambda kv: -kv[1]["saved_tok"]):
        pct = min(99.9, c["saved_tok"] / max(1, c["raw_tok"]) * 100.0)
        out.append({"cat": cat, "saved_tok": c["saved_tok"], "events": c["events"], "pct": round(pct, 1)})
    return out


def _is_antigravity_transcript(path: Path) -> bool:
    return path.name in ("transcript.jsonl", "transcript_full.jsonl") and path.parent.name == "logs"


def snapshot(transcript: str = "", session_id: str = "", cwd: str = "", host: str = "") -> dict[str, Any]:
    from runtime import analytics, autopilot

    cfg = load_config()
    price = analytics.pricing(cfg)
    reread = analytics.DEFAULT_REREAD_TURNS
    effective_host = (host or os.environ.get("TERSER_CLIENT_ID") or "").strip().lower()

    if not transcript and effective_host == "antigravity":
        found = None
        if session_id:
            ag_home = (os.environ.get("TOKEN_OPTIMIZER_ANTIGRAVITY_HOME") or "").strip()
            base = Path(os.path.expanduser(ag_home)) if ag_home else (Path.home() / ".gemini")
            for surf in ("antigravity", "antigravity-cli", "antigravity-ide"):
                logs_dir = base / surf / "brain" / session_id / ".system_generated" / "logs"
                found = analytics._preferred_ag_transcript(logs_dir)
                if found is not None:
                    break
        if found is None:
            found = analytics.latest_antigravity_transcript()
        if found is not None:
            transcript = str(found)

    l_events, _l_raw, l_saved, l_cats = _savings()
    out: dict[str, Any] = {
        "version": __version__,
        "generated_at": time.time(),
        "host": effective_host or "claude",
        "enabled": bool(cfg.get("enabled", True)),
        "shadow": bool(cfg.get("shadow", False)),
        "price": {
            "input_per_mtok": price["price_input_per_mtok"],
            "output_per_mtok": price["price_output_per_mtok"],
            "cache_write_mult": price["cache_write_mult"],
            "cache_read_mult": price["cache_read_mult"],
            "reread_turns": reread,
        },
        "lifetime": {
            "saved_tok": l_saved,
            "saved_usd": round(analytics.saved_cost(l_saved, price, reread), 4),
            "events": l_events,
            "by_cat": _cats(l_cats),
        },
        "features": {
            k: cfg.get(k)
            for k in (
                "read_cache", "delta_diff", "outline_first", "form_repair", "bash_compress",
                "cmd_delta", "fts_sandbox", "loop_guard", "mcp_refetch_guard", "repo_map",
                "index_auto_reindex", "toolprune", "code_discipline",
            )
            if k in cfg
        },
        "session": None,
        "index": None,
        "funnel": [],
    }

    if not session_id and transcript:
        tp = Path(transcript)
        if _is_antigravity_transcript(tp) and len(tp.parents) >= 3:
            session_id = tp.parents[2].name
        else:
            session_id = tp.stem
    sess: dict[str, Any] = {}
    started = _started_at(session_id) if session_id else None
    if transcript and Path(transcript).is_file():
        try:
            tp = Path(transcript)
            is_ag = _is_antigravity_transcript(tp) or effective_host == "antigravity"
            st = analytics.parse_antigravity_transcript(tp) if is_ag else analytics.parse_claude_transcript(tp)
            if not started and st.started:
                started = st.started
            served, retried = analytics.funnel_retry_ratio(st.started or 0.0)
            letter, score, reasons = analytics.quality_grade(st, served=served, retried=retried)
            window = analytics.context_window(st)
            fill = autopilot.context_fill(transcript, "antigravity" if is_ag else "claude")
            model_price = analytics.pricing(cfg, model=st.model)
            cost = analytics.session_cost(st, model_price)
            sess.update(
                session_id=st.session_id or session_id,
                model=st.model,
                turns=st.turns,
                input_tok=st.input_tokens,
                output_tok=st.output_tokens,
                cache_read_tok=st.cache_read,
                cache_write_tok=st.cache_write,
                cost_usd=round(cost, 4),
                ctx_fill=round(fill, 4) if fill is not None else None,
                ctx_window=window,
                peak_ctx=st.peak_context,
                tool_calls=st.total_calls,
                tool_errors=st.tool_errors,
                top_tools=[{"tool": t, "n": n} for t, n in st.tool_calls.most_common(8)],
                repeated_reads=st.repeated_reads,
                quick_reruns=st.quick_reruns,
                archive_expands=st.archive_expands,
                big_results=st.big_results,
                grade={"letter": letter, "score": score, "reasons": reasons},
                started=st.started,
                ended=st.ended,
            )
            # Cache hit ratio: share of prompt tokens served from cache.
            prompt = st.input_tokens + st.cache_read + st.cache_write
            sess["cache_hit"] = round(st.cache_read / prompt, 4) if prompt else None
        except Exception as exc:  # a broken transcript must not break the band
            sess["error"] = f"transcript: {exc.__class__.__name__}"
    if started:
        s_events, _s_raw, s_saved, s_cats = _savings(started)
        sess.update(
            started_at=started,
            saved_tok=s_saved,
            saved_usd=round(analytics.saved_cost(s_saved, price, reread), 4),
            events=s_events,
            by_cat=_cats(s_cats),
        )
    out["session"] = sess or None

    if cwd:
        try:
            from runtime import index_bridge

            idx = index_bridge.workspace_index_summary(cwd)
            if idx:
                out["index"] = {"repo": idx.get("repo"), "files": idx.get("files"), "symbols": idx.get("symbols")}
        except Exception:
            pass
    is_ag_host = (effective_host or "claude") == "antigravity"
    routing: dict[str, Any] = {
        "enabled": bool(cfg.get("subagent_model_routing", False)),
        "model": str(
            cfg.get("subagent_cheap_model_antigravity") or "flash"
            if is_ag_host
            else cfg.get("subagent_cheap_model") or "haiku"
        ),
        "effort": str(
            cfg.get("subagent_cheap_effort_antigravity") or ""
            if is_ag_host
            else cfg.get("subagent_cheap_effort") or ""
        ),
        "verify": bool(cfg.get("subagent_verify", True)),
        "model_key": "subagent_cheap_model_antigravity" if is_ag_host else "subagent_cheap_model",
        "effort_key": "subagent_cheap_effort_antigravity" if is_ag_host else "subagent_cheap_effort",
    }
    try:
        from runtime import subagent_verify as sv

        st = sv.stats()
        routing.update({k: st.get(k) for k in ("routed", "checked", "failed", "backed_off") if k in st})
        routing["available_models"] = list(sv.AGY_SUBAGENT_MODELS if is_ag_host else sv.CLAUDE_SUBAGENT_MODELS)
        routing["available_efforts"] = list(sv.AGY_EFFORT_LEVELS if is_ag_host else sv.EFFORT_LEVELS)
    except Exception:
        pass
    out["routing"] = routing
    try:
        for tier, c in sorted(load_funnel_summary().items()):
            out["funnel"].append({"tier": tier, **{k: int(v or 0) for k, v in c.items()}})
    except Exception:
        pass
    return out


HEARTBEAT = "hud-heartbeat"
HEARTBEAT_TTL_S = 300.0


def is_active(env: dict[str, str] | None = None) -> bool:
    """True while the desktop status band has pulled a snapshot in the last few minutes."""
    try:
        return time.time() - (state_home(env) / HEARTBEAT).stat().st_mtime < HEARTBEAT_TTL_S
    except OSError:
        return False


def _touch_heartbeat() -> None:
    try:
        p = state_home() / HEARTBEAT
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    transcript = session_id = cwd = host = ""
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--transcript", "--session", "--cwd", "--host") and i + 1 < len(argv):
            val = argv[i + 1]
            if a == "--transcript":
                transcript = val
            elif a == "--session":
                session_id = val
            elif a == "--host":
                host = val
            else:
                cwd = val
            i += 2
        else:
            i += 1
    if "--json" in argv:  # only the band asks with --json; it makes the Stop-hook notice redundant
        _touch_heartbeat()
    try:
        data = snapshot(transcript, session_id, cwd, host=host)
    except Exception as exc:
        data = {"error": f"{exc.__class__.__name__}: {exc}"}
    sys.stdout.write(json.dumps(data, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
