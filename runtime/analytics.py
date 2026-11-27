"""Terser native analytics: session audit, quality grade, coaching, dashboard, compaction.

Data sources (all local, read-only except Terser's own state dir):

* Claude Code transcripts  ``~/.claude/projects/*/*.jsonl`` (or ``CLAUDE_CONFIG_DIR``)
* Codex rollouts           ``~/.codex/sessions/**/rollout-*.jsonl`` (or ``CODEX_HOME``)
* Terser logs              ``savings.jsonl`` and ``funnel.jsonl`` under ``TERSER_HOME``

Cost figures are estimates under a prompt-cached billing model whose assumptions are
printed with every report and can be overridden in config (``price_input_per_mtok``,
``price_output_per_mtok``, ``cache_read_mult``, ``cache_write_mult``).
"""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
import webbrowser
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_THIS = Path(__file__).resolve()
if str(_THIS.parents[1]) not in sys.path:
    sys.path.insert(0, str(_THIS.parents[1]))

from runtime.config import (  # noqa: E402
    estimate_tokens,
    load_config,
    load_funnel_summary,
    normalize_workspace,
    state_home,
    workspace_hash,
)
from runtime import activity as _activity  # noqa: E402

DEFAULT_REREAD_TURNS = 3.0
_PRICING_DEFAULTS = {
    "price_input_per_mtok": 3.0,
    "price_output_per_mtok": 15.0,
    "cache_read_mult": 0.10,
    "cache_write_mult": 1.25,
}
_READ_TOOLS = {"Read", "read_file", "view_file", "NotebookRead"}
_EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit", "edit_file", "write_file", "apply_patch"}
_SHELL_TOOLS = {"Bash", "PowerShell", "run_command", "shell", "exec_command"}
_BIG_RESULT_CHARS = 20_000
_RERUN_WINDOW_S = 120.0
_EXPAND_TOOL_KEYS = ("expand_sandbox", "search_sandbox")
# `terse expand|query` (or `cli.py expand|query`) as the program of a command segment, not
# inside a grep pattern or an echo that merely mentions it.
_EXPAND_CMD_RE = re.compile(
    r"^(?:[A-Za-z_]\w*=\S*\s+)*"
    r"(?:\S*[/\\])?(?:terse(?:\.cmd|\.py)?|(?:python3?(?:\.exe)?\s+(?:\S+\s+)*?)?\S*cli\.py[\"']?)"
    r"\s+(?:expand|query)\b"
)
_CMD_SPLIT_RE = re.compile(r"&&|\|\||;|\n")
# Text Terser itself puts into tool output; a result carrying one already went through Terser.
_TERSER_MARKER_RE = re.compile(r"\[Terser|terse (?:expand|query) [0-9a-f]{6,}|(?:expand|search)_sandbox\b.{0,40}[0-9a-f]{6,}|Terser: identical")


def _is_expand_call(name: str, inp: Any) -> bool:
    low = name.lower()
    if any(k in low for k in _EXPAND_TOOL_KEYS):
        return True
    # terser-index routes archive retrieval through `order(action="expand_sandbox"|"search_sandbox")`.
    if isinstance(inp, dict) and str(inp.get("action") or "").lower() in _EXPAND_TOOL_KEYS:
        return True
    if name in _SHELL_TOOLS or low in ("shell", "exec_command"):
        cmd = inp.get("command") if isinstance(inp, dict) else None
        cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd or "")
        return any(_EXPAND_CMD_RE.match(seg.strip().lstrip("(")) for seg in _CMD_SPLIT_RE.split(cmd))
    return False


def _has_terser_marker(text: str) -> bool:
    """Terser frames its output at the head (hook notices) or tail (archive pointers).

    Only those edges are checked, so reading or grepping Terser's own source, which
    contains the same strings, is not mistaken for Terser-processed output.
    """
    edges = text[:300] + "\n" + text[-600:] if len(text) > 900 else text
    return bool(_TERSER_MARKER_RE.search(edges))


# ---------------------------------------------------------------------------
# Transcript parsing
# ---------------------------------------------------------------------------

@dataclass
class SessionStats:
    path: str
    host: str
    session_id: str = ""
    cwd: str = ""
    started: float = 0.0
    ended: float = 0.0
    turns: int = 0
    input_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output_tokens: int = 0
    peak_context: int = 0
    tool_calls: Counter = field(default_factory=Counter)
    tool_errors: int = 0
    big_results: int = 0
    reads: Counter = field(default_factory=Counter)
    edited: list = field(default_factory=list)
    shell_cmds: list = field(default_factory=list)
    prompts: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    model: str = ""
    # Regret signals: [ts, full command, output carried a Terser marker] per shell run,
    # plus calls that pulled archived output back (expand_sandbox / search_sandbox / terse expand).
    shell_runs: list = field(default_factory=list)
    archive_expands: int = 0
    # Ordered (tool name, short input summary) per call, for activity-mode detection.
    tool_seq: list = field(default_factory=list)
    # Decision statements from user prompts and assistant text (see runtime/activity.py).
    decisions: list = field(default_factory=list)

    def _reruns(self) -> list[bool]:
        """For each quick byte-identical re-run, whether the earlier run's output went through Terser."""
        last: dict[str, tuple[float, bool]] = {}
        out: list[bool] = []
        for ts, cmd, compressed in self.shell_runs:
            if not cmd:
                continue
            prev = last.get(cmd)
            if prev is not None and ts and prev[0] and 0 <= ts - prev[0] <= _RERUN_WINDOW_S:
                out.append(prev[1])
            last[cmd] = (ts, compressed)
        return out

    @property
    def quick_reruns(self) -> int:
        return len(self._reruns())

    @property
    def terser_reruns(self) -> int:
        return sum(1 for c in self._reruns() if c)

    @property
    def repeated_reads(self) -> int:
        return sum(n - 1 for n in self.reads.values() if n > 1)

    @property
    def repeated_cmds(self) -> int:
        return sum(1 for a, b in zip(self.shell_cmds, self.shell_cmds[1:]) if a == b)

    @property
    def total_calls(self) -> int:
        return sum(self.tool_calls.values())


def _ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    yield rec
    except OSError:
        return


def _short(text: Any, n: int = 140) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 3] + "..."


def parse_claude_transcript(path: Path) -> SessionStats:
    st = SessionStats(path=str(path), host="claude")
    tool_names: dict[str, str] = {}
    run_idx: dict[str, int] = {}
    for rec in _iter_jsonl(path):
        ts = _ts(rec.get("timestamp"))
        if ts:
            st.started = st.started or ts
            st.ended = max(st.ended, ts)
        st.session_id = st.session_id or str(rec.get("sessionId") or "")
        st.cwd = st.cwd or str(rec.get("cwd") or "")
        if rec.get("isSidechain"):
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if rec.get("type") == "assistant":
            st.model = st.model or str(msg.get("model") or "")
            usage = msg.get("usage") or {}
            if isinstance(usage, dict) and usage:
                st.turns += 1
                st.input_tokens += int(usage.get("input_tokens") or 0)
                st.cache_read += int(usage.get("cache_read_input_tokens") or 0)
                st.cache_write += int(usage.get("cache_creation_input_tokens") or 0)
                st.output_tokens += int(usage.get("output_tokens") or 0)
                ctx = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                st.peak_context = max(st.peak_context, ctx)
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "text":
                    _activity.collect_decisions(str(block.get("text") or ""), st.decisions, source="assistant")
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name") or "")
                tool_names[str(block.get("id") or "")] = name
                st.tool_calls[name] += 1
                inp = block.get("input") or {}
                st.tool_seq.append((name, _activity.input_summary(inp)))
                if not isinstance(inp, dict):
                    continue
                if _is_expand_call(name, inp):
                    st.archive_expands += 1
                fp = str(inp.get("file_path") or inp.get("path") or "")
                if name in _READ_TOOLS and fp and not inp.get("offset"):
                    st.reads[fp] += 1
                elif name in _EDIT_TOOLS and fp:
                    if fp not in st.edited:
                        st.edited.append(fp)
                elif name in _SHELL_TOOLS:
                    cmd = str(inp.get("command") or "")
                    st.shell_cmds.append(cmd[:300])
                    run_idx[str(block.get("id") or "")] = len(st.shell_runs)
                    st.shell_runs.append([ts, cmd, False])
        elif rec.get("type") == "user":
            if isinstance(content, str):
                if not content.startswith("<"):
                    st.prompts.append(_short(content, 200))
                    _activity.collect_decisions(content, st.decisions, source="user")
                continue
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and not str(block.get("text", "")).startswith("<"):
                    st.prompts.append(_short(block.get("text"), 200))
                    _activity.collect_decisions(str(block.get("text") or ""), st.decisions, source="user")
                if block.get("type") != "tool_result":
                    continue
                body = block.get("content")
                text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
                if len(text) > _BIG_RESULT_CHARS:
                    st.big_results += 1
                ri = run_idx.get(str(block.get("tool_use_id") or ""))
                if ri is not None and _has_terser_marker(text):
                    st.shell_runs[ri][2] = True
                if block.get("is_error"):
                    st.tool_errors += 1
                    tool = tool_names.get(str(block.get("tool_use_id") or ""), "tool")
                    st.errors.append(f"{tool}: {_short(text, 120)}")
    return st


def _unwrap_ag_arg(val: Any) -> Any:
    """Antigravity transcript tool_calls store scalar args as JSON-encoded strings."""
    if isinstance(val, str) and len(val) >= 2 and val[0] == '"' and val[-1] == '"':
        try:
            return json.loads(val)
        except ValueError:
            return val
    return val


def _preferred_ag_transcript(logs_dir: Path) -> Path | None:
    """Prefer full Antigravity transcript over truncated transcript when present."""
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        cand = logs_dir / name
        if cand.is_file():
            return cand
    return None


def latest_antigravity_transcript(env: dict[str, str] | None = None) -> Path | None:
    """Find the most recently modified Antigravity transcript across all 3 surfaces."""
    e = env if env is not None else os.environ
    custom = (e.get("ANTIGRAVITY_BRAIN_DIR") or "").strip()
    if custom:
        brains = [Path(os.path.expanduser(custom))]
    else:
        ag_home = (e.get("TOKEN_OPTIMIZER_ANTIGRAVITY_HOME") or "").strip()
        base = Path(os.path.expanduser(ag_home)) if ag_home else (Path.home() / ".gemini")
        brains = [base / surf / "brain" for surf in ("antigravity", "antigravity-cli", "antigravity-ide")]
    best: tuple[float, Path] | None = None
    for brain in brains:
        if not brain.is_dir():
            continue
        try:
            for conv in brain.iterdir():
                if not conv.is_dir():
                    continue
                cand = _preferred_ag_transcript(conv / ".system_generated" / "logs")
                if cand is None:
                    continue
                try:
                    mtime = cand.stat().st_mtime
                except OSError:
                    continue
                if best is None or mtime > best[0]:
                    best = (mtime, cand)
        except OSError:
            continue
    return best[1] if best is not None else None


def parse_antigravity_transcript(path: Path) -> SessionStats:
    """Parse an Antigravity 2.0 `.system_generated/logs/transcript.jsonl` file."""
    st = SessionStats(path=str(path), host="antigravity")
    try:
        if path.parent.name == "logs" and path.parent.parent.name == ".system_generated":
            st.session_id = path.parents[2].name
    except IndexError:
        pass
    for rec in _iter_jsonl(path):
        ts = _ts(rec.get("created_at") or rec.get("timestamp"))
        if ts:
            st.started = st.started or ts
            st.ended = max(st.ended, ts)
        rtype = str(rec.get("type") or "")
        if rtype == "PLANNER_RESPONSE":
            model = str(rec.get("model") or "")
            if model:
                st.model = model
            inp = int(rec.get("input_tokens") or 0)
            cr = int(rec.get("cache_read_tokens") or 0)
            cw = int(rec.get("cache_write_tokens") or 0)
            out = int(rec.get("output_tokens") or 0)
            if inp or cr or cw or out:
                st.turns += 1
                st.input_tokens += inp
                st.cache_read += cr
                st.cache_write += cw
                st.output_tokens += out
                st.peak_context = max(st.peak_context, inp + cr + cw)
            for tc in rec.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                name = str(tc.get("name") or "")
                if not name:
                    continue
                st.tool_calls[name] += 1
                raw_args = tc.get("args")
                args = {k: _unwrap_ag_arg(v) for k, v in raw_args.items()} if isinstance(raw_args, dict) else {}
                st.tool_seq.append((name, _activity.input_summary(args)))
                if _is_expand_call(name, args):
                    st.archive_expands += 1
                fp = str(args.get("Path") or args.get("file_path") or args.get("path") or "")
                has_range = bool(args.get("StartLine") or args.get("EndLine") or args.get("offset"))
                if name in _READ_TOOLS and fp and not has_range:
                    st.reads[fp] += 1
                elif name in _EDIT_TOOLS and fp:
                    if fp not in st.edited:
                        st.edited.append(fp)
                elif name in _SHELL_TOOLS:
                    cmd = str(args.get("CommandLine") or args.get("command") or "")
                    st.shell_cmds.append(cmd[:300])
                    st.shell_runs.append([ts, cmd, False])
        elif rtype == "GENERIC":
            text = str(rec.get("content") or "")
            if len(text) > _BIG_RESULT_CHARS:
                st.big_results += 1
            if st.shell_runs and _has_terser_marker(text):
                st.shell_runs[-1][2] = True
            err = str(rec.get("error") or "")
            if (rec.get("status") == "ERROR" or err) and "[Terser]" not in err:
                st.tool_errors += 1
                st.errors.append(_short(err or text, 120))
        elif rtype == "USER_INPUT":
            body = str(rec.get("content") or "")
            m = re.search(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", body, re.DOTALL)
            prompt = m.group(1) if m else body
            if prompt.strip():
                st.prompts.append(_short(prompt, 200))
    return st


def _codex_args(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("arguments") or payload.get("input") or ""
    if isinstance(raw, dict):
        return raw
    try:
        args = json.loads(raw) if isinstance(raw, str) and raw.lstrip().startswith("{") else {}
    except ValueError:
        args = {}
    return args if isinstance(args, dict) else {}


def _codex_output_text(out: Any) -> str:
    """Codex tool output is a string, sometimes a JSON string wrapping ``{"output": ...}``."""
    if isinstance(out, dict):
        return str(out.get("output") or out.get("content") or json.dumps(out, ensure_ascii=False))
    text = str(out or "")
    if text.startswith("{") and '"output"' in text[:200]:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and isinstance(obj.get("output"), str):
                return obj["output"]
        except ValueError:
            pass
    return text


def parse_codex_rollout(path: Path) -> SessionStats:
    st = SessionStats(path=str(path), host="codex")
    last_total: dict[str, Any] = {}
    run_idx: dict[str, int] = {}
    for rec in _iter_jsonl(path):
        ts = _ts(rec.get("timestamp"))
        if ts:
            st.started = st.started or ts
            st.ended = max(st.ended, ts)
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        kind = rec.get("type")
        if kind == "session_meta":
            st.session_id = str(payload.get("id") or "")
            st.cwd = str(payload.get("cwd") or "")
        elif kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            if isinstance(info, dict):
                last_total = info.get("total_token_usage") or last_total
                last = info.get("last_token_usage") or {}
                st.peak_context = max(st.peak_context, int(last.get("input_tokens") or 0))
                st.turns += 1
        elif kind == "event_msg" and payload.get("type") == "user_message":
            st.prompts.append(_short(payload.get("message"), 200))
        elif kind == "response_item" and payload.get("type") in ("function_call", "custom_tool_call"):
            name = str(payload.get("name") or "")
            st.tool_calls[name] += 1
            args = _codex_args(payload)
            if _is_expand_call(name, args):
                st.archive_expands += 1
            cmd = args.get("command") if isinstance(args, dict) else None
            if cmd:
                full = " ".join(cmd) if isinstance(cmd, list) else str(cmd)
                st.shell_cmds.append(full[:300])
                run_idx[str(payload.get("call_id") or "")] = len(st.shell_runs)
                st.shell_runs.append([ts, full, False])
        elif kind == "response_item" and payload.get("type") in ("function_call_output", "custom_tool_call_output"):
            out = _codex_output_text(payload.get("output"))
            if len(out) > _BIG_RESULT_CHARS:
                st.big_results += 1
            ri = run_idx.get(str(payload.get("call_id") or ""))
            if ri is not None and _has_terser_marker(out):
                st.shell_runs[ri][2] = True
    if last_total:
        cached = int(last_total.get("cached_input_tokens") or 0)
        st.input_tokens = max(0, int(last_total.get("input_tokens") or 0) - cached)
        st.cache_read = cached
        st.output_tokens = int(last_total.get("output_tokens") or 0)
    return st


def _transcript_roots(host: str, env: dict[str, str]) -> list[tuple[str, Path, str]]:
    roots: list[tuple[str, Path, str]] = []
    if host in ("claude", "all"):
        base = Path(os.path.expanduser(env.get("CLAUDE_CONFIG_DIR") or "~/.claude")) / "projects"
        roots.append(("claude", base, "*/*.jsonl"))
    if host in ("codex", "all"):
        base = Path(os.path.expanduser(env.get("CODEX_HOME") or "~/.codex")) / "sessions"
        roots.append(("codex", base, "**/rollout-*.jsonl"))
    return roots


def _recent_files(host: str, days: float, limit: int, env: dict[str, str] | None) -> list[tuple[float, str, Path]]:
    env = env if env is not None else dict(os.environ)
    cutoff = time.time() - days * 86400
    found: list[tuple[float, str, Path]] = []
    for kind, base, pattern in _transcript_roots(host, env):
        if not base.is_dir():
            continue
        for p in base.glob(pattern):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt >= cutoff:
                found.append((mt, kind, p))
    found.sort(reverse=True)
    return found[:limit]


def recent_sessions(host: str, days: float = 7.0, limit: int = 40, env: dict[str, str] | None = None) -> list[SessionStats]:
    out = []
    for _, kind, p in _recent_files(host, days, limit, env):
        st = parse_claude_transcript(p) if kind == "claude" else parse_codex_rollout(p)
        if st.turns:
            out.append(st)
    return out


# ---------------------------------------------------------------------------
# Cost, quality, Terser logs
# ---------------------------------------------------------------------------

_MODEL_PRICING: tuple[tuple[str, dict[str, float]], ...] = (
    ("flash_lite", {"price_input_per_mtok": 0.10, "price_output_per_mtok": 0.40, "cache_read_mult": 0.10, "cache_write_mult": 0.0}),
    ("flash-lite", {"price_input_per_mtok": 0.10, "price_output_per_mtok": 0.40, "cache_read_mult": 0.10, "cache_write_mult": 0.0}),
    ("gemini", {"price_input_per_mtok": 1.25, "price_output_per_mtok": 10.0, "cache_read_mult": 0.10, "cache_write_mult": 0.0}),
    ("haiku", {"price_input_per_mtok": 0.80, "price_output_per_mtok": 4.0, "cache_read_mult": 0.10, "cache_write_mult": 1.25}),
    ("sonnet", {"price_input_per_mtok": 3.0, "price_output_per_mtok": 15.0, "cache_read_mult": 0.10, "cache_write_mult": 1.25}),
    ("opus", {"price_input_per_mtok": 15.0, "price_output_per_mtok": 75.0, "cache_read_mult": 0.10, "cache_write_mult": 1.25}),
    ("gpt-5", {"price_input_per_mtok": 2.50, "price_output_per_mtok": 15.0, "cache_read_mult": 0.10, "cache_write_mult": 0.0}),
    ("codex", {"price_input_per_mtok": 1.50, "price_output_per_mtok": 6.0, "cache_read_mult": 0.10, "cache_write_mult": 0.0}),
)


def _model_pricing_defaults(model: str | None) -> dict[str, float]:
    m = (model or "").strip().lower()
    if m:
        if "flash" in m and "lite" not in m:
            return {"price_input_per_mtok": 0.30, "price_output_per_mtok": 2.50, "cache_read_mult": 0.10, "cache_write_mult": 0.0}
        for needle, rates in _MODEL_PRICING:
            if needle in m:
                return dict(rates)
    return dict(_PRICING_DEFAULTS)


def pricing(
    cfg: dict[str, Any] | None = None,
    *,
    price_in: float | None = None,
    model: str | None = None,
) -> dict[str, float]:
    """Price model: model-aware defaults, then config.json overrides, then explicit ``--price-in``."""
    cfg = cfg if cfg is not None else load_config()
    out = _model_pricing_defaults(model)
    for k in out:
        try:
            if cfg.get(k) is not None:
                out[k] = float(cfg[k])
        except (TypeError, ValueError):
            pass
    if price_in is not None and price_in >= 0:
        out["price_input_per_mtok"] = float(price_in)
    return out


def session_cost(st: SessionStats, price: dict[str, float]) -> float:
    pin = price["price_input_per_mtok"] / 1e6
    return (
        st.input_tokens * pin
        + st.cache_write * pin * price["cache_write_mult"]
        + st.cache_read * pin * price["cache_read_mult"]
        + st.output_tokens * price["price_output_per_mtok"] / 1e6
    )


def saved_cost(saved_tokens: int, price: dict[str, float], resend_turns: float = DEFAULT_REREAD_TURNS) -> float:
    """Conservative value of tokens kept out of context.

    A kept-out token would have been written to the prompt cache once and re-read on
    roughly ``resend_turns`` later turns, so it is valued at the cached rates rather
    than the full input price.
    """
    pin = price["price_input_per_mtok"] / 1e6
    return saved_tokens * pin * (price["cache_write_mult"] + resend_turns * price["cache_read_mult"])


def _load_jsonl(name: str, since: float, env: dict[str, str] | None) -> list[dict[str, Any]]:
    p = state_home(env) / name
    if not p.is_file():
        return []
    return [r for r in _iter_jsonl(p) if float(r.get("ts") or 0) >= since]


def funnel_retry_ratio(since: float, env: dict[str, str] | None = None) -> tuple[int, int]:
    served = retried = 0
    for r in _load_jsonl("funnel.jsonl", since, env):
        if r.get("ev") == "served":
            served += 1
        elif r.get("ev") == "retry":
            retried += 1
    return served, retried


def context_window(st: SessionStats) -> int:
    """Best-effort window: 1M for Gemini / [1m] models or when usage exceeded 200k."""
    m = (st.model or "").lower()
    if "[1m]" in m or "gemini" in m or st.peak_context > 200_000:
        return 1_000_000
    return 200_000


def quality_grade(st: SessionStats | None, *, served: int = 0, retried: int = 0) -> tuple[str, int, list[str]]:
    """Score a session 0-100 on avoidable rework; return (letter, score, reasons)."""
    score = 100.0
    reasons: list[str] = []
    if st is not None and st.total_calls:
        err_rate = st.tool_errors / st.total_calls
        if err_rate > 0.05:
            pen = min(25.0, (err_rate - 0.05) * 150)
            score -= pen
            reasons.append(f"{st.tool_errors} failed tool call(s) ({err_rate:.0%})")
        if st.repeated_reads:
            pen = min(20.0, st.repeated_reads * 2.0)
            score -= pen
            reasons.append(f"{st.repeated_reads} repeated full-file read(s)")
        if st.repeated_cmds:
            score -= min(15.0, st.repeated_cmds * 3.0)
            reasons.append(f"{st.repeated_cmds} back-to-back identical command(s)")
        if st.big_results:
            score -= min(15.0, st.big_results * 3.0)
            reasons.append(f"{st.big_results} tool result(s) over {_BIG_RESULT_CHARS // 1000}K chars")
        window = context_window(st)
        if st.host == "claude" and st.peak_context > 0.85 * window:
            score -= 10
            reasons.append(f"context peaked at {st.peak_context / window:.0%}")
    if served >= 5 and retried / served > 0.3:
        score -= min(15.0, (retried / served - 0.3) * 50)
        reasons.append(f"{retried}/{served} short read answers were retried")
    score = max(0, round(score))
    letter = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 60 else "F"
    return letter, score, reasons


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _fmt_tok(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


def _assumptions(price: dict[str, float], reread_turns: float = DEFAULT_REREAD_TURNS) -> str:
    per_tok = price["cache_write_mult"] + reread_turns * price["cache_read_mult"]
    return (
        f"Assumptions: input ${price['price_input_per_mtok']:.2f}/MTok, output ${price['price_output_per_mtok']:.2f}/MTok, "
        f"cache write x{price['cache_write_mult']}, cache read x{price['cache_read_mult']}; "
        f"a kept-out token is valued as 1 cache write + {reread_turns:g} cache re-read(s) "
        f"= x{per_tok:.2f} input price "
        "(override with --price-in <$/MTok> --reread-turns <N>)."
    )


def regret_lines(sessions: list[SessionStats], indent: str = "  ") -> list[str]:
    """Signs that compression dropped something the agent then had to fetch again."""
    reruns = sum(s.quick_reruns for s in sessions)
    t_reruns = sum(s.terser_reruns for s in sessions)
    expands = sum(s.archive_expands for s in sessions)
    shell = sum(len(s.shell_runs) for s in sessions)
    line = (
        f"{indent}Regret signals: {reruns} quick identical re-run(s) within {_RERUN_WINDOW_S / 60:g} min "
        f"({t_reruns} after a Terser-compressed output), {expands} archive expand(s)"
    )
    if shell:
        line += f" across {shell} shell call(s)"
    out = [line + "."]
    if shell and (t_reruns + expands) / shell > 0.05:
        out.append(f"{indent}  High regret: compression may be dropping needed lines; try `terse config set enhanced_compression=false` for a session and compare.")
    return out


def report(
    host: str,
    days: float = 7.0,
    env: dict[str, str] | None = None,
    *,
    price_in: float | None = None,
    reread_turns: float = DEFAULT_REREAD_TURNS,
) -> str:
    price = pricing(price_in=price_in)
    since = time.time() - days * 86400
    sessions = recent_sessions(host, days, env=env)
    savings = _load_jsonl("savings.jsonl", since, env)
    saved = sum(int(r.get("saved_tok") or 0) for r in savings if r.get("host") in (host, None) or host == "all")
    served, retried = funnel_retry_ratio(since, env)
    lines = [f"Terser audit — last {days:g} day(s), host={host}"]
    if not sessions:
        lines.append("  No transcripts found for this host in the window.")
    else:
        tot = SessionStats(path="", host=host)
        for s in sessions:
            for f in ("input_tokens", "cache_read", "cache_write", "output_tokens", "tool_errors", "big_results", "turns"):
                setattr(tot, f, getattr(tot, f) + getattr(s, f))
        cost = sum(session_cost(s, price) for s in sessions)
        lines.append(
            f"  {len(sessions)} session(s), {tot.turns} turns: input {_fmt_tok(tot.input_tokens)}, "
            f"cache read {_fmt_tok(tot.cache_read)}, cache write {_fmt_tok(tot.cache_write)}, "
            f"output {_fmt_tok(tot.output_tokens)} — est. ${cost:.2f}"
        )
        hit = tot.cache_read / max(1, tot.cache_read + tot.cache_write + tot.input_tokens)
        lines.append(f"  Prompt-cache hit rate: {hit:.0%}")
        lines.append("")
        lines.append("  Session                              Turns  Est.$   Grade  Top issue")
        for s in sessions[:12]:
            letter, score, reasons = quality_grade(s)
            name = Path(s.cwd).name or Path(s.path).stem[:12]
            label = f"{time.strftime('%m-%d %H:%M', time.localtime(s.started or 0))} {name}"[:36]
            lines.append(f"  {label:<36} {s.turns:>5}  {session_cost(s, price):>6.2f}  {letter} {score:>3}  {reasons[0] if reasons else '-'}")
        lines.append("")
        lines.extend(regret_lines(sessions))
    lines.append("")
    lines.append(
        f"  Terser kept ~{_fmt_tok(saved)} tokens out of context "
        f"(~${saved_cost(saved, price, reread_turns):.2f} at cached rates)."
    )
    if served:
        lines.append(f"  Read funnel: {served} short answers, {retried} retried ({retried / served:.0%}).")
    lines.append("  " + _assumptions(price, reread_turns))
    lines.append("  Counterfactual for sessions Terser did not touch: `terse discover --replay`.")
    return "\n".join(lines)


def coach(host: str, days: float = 7.0, env: dict[str, str] | None = None) -> str:
    sessions = recent_sessions(host, days, env=env)
    tips: list[str] = []
    reads: Counter = Counter()
    errs: Counter = Counter()
    cmds: Counter = Counter()
    for s in sessions:
        reads.update({k: v - 1 for k, v in s.reads.items() if v > 1})
        errs.update(e.split(":", 1)[0] for e in s.errors)
        cmds.update(c for c in s.shell_cmds if c)
    if reads:
        f, n = reads.most_common(1)[0]
        tips.append(f"`{Path(f).name}` was re-read {n} extra time(s). Use terser-index `get_symbol_source` or ranged reads instead of full re-reads.")
    if errs:
        t, n = errs.most_common(1)[0]
        tips.append(f"{n} failed `{t}` call(s). Check the session's errors with `terse audit`; repeated failures usually mean a wrong path or stale assumption.")
    heavy = [c for c, n in cmds.most_common(5) if n >= 5]
    if heavy:
        tips.append(f"`{_short(heavy[0], 60)}` ran {cmds[heavy[0]]} times. Scope it (paths, -q, --oneline) or rely on Terser's command delta.")
    big = sum(s.big_results for s in sessions)
    if big:
        tips.append(f"{big} tool result(s) exceeded {_BIG_RESULT_CHARS // 1000}K chars. Prefer `execute_filtered` with an intent, or pipe through head/grep.")
    peak = [s for s in sessions if s.host == "claude" and s.peak_context > 0.85 * context_window(s)]
    if peak:
        tips.append(f"{len(peak)} session(s) ran near the context limit. Start fresh sessions per task, or delegate broad searches to subagents.")
    cfg = load_config()
    for key, tip in (("outline_first", "Outline-first reads are off; large files are read whole."),
                     ("bash_compress", "Shell output compression is off."),
                     ("read_cache", "The read cache is off; unchanged re-reads are sent in full.")):
        if not cfg.get(key, True):
            tips.append(f"{tip} Re-enable with `terse config set {key}=true`.")
    if not tips:
        return f"Terser coach: no waste patterns found in {len(sessions)} session(s) over {days:g} day(s)."
    return "Terser coach:\n" + "\n".join(f"  {i}. {t}" for i, t in enumerate(tips, 1))


# ---------------------------------------------------------------------------
# Discover --replay: counterfactual through Terser's own compressors
# ---------------------------------------------------------------------------

REPLAY_CATEGORIES = ("shell", "read", "mcp", "terser", "other")
_REPLAY_LABELS = {
    "shell": "Shell output",
    "read": "File reads",
    "mcp": "MCP results",
    "terser": "Already via Terser",
    "other": "Other tools",
}
_READ_LINE_PREFIX = re.compile(r"^\s*\d+(?:\t|→)", re.M)
_SHELL_MIN_CHARS = 300
_MCP_LOSSLESS_MIN_CHARS = 1500
_KEEP_RATIO = 0.85  # the hook only swaps output when it shrinks below 85% of raw
_MIN_RETRY_SAMPLE = 10  # outline retry rate is applied only once this many outlines were measured


@dataclass
class ToolOutput:
    host: str
    name: str
    inp: dict
    text: str
    is_error: bool = False
    cwd: str = ""


def _claude_result_text(body: Any) -> str:
    if isinstance(body, str):
        return body
    if isinstance(body, list):
        return "\n".join(str(b.get("text") or "") for b in body if isinstance(b, dict) and b.get("type") == "text")
    return "" if body is None else json.dumps(body, ensure_ascii=False)


def iter_tool_outputs(path: Path, kind: str) -> Iterable[ToolOutput]:
    """Pair each tool call with its textual result. Images and non-text blocks are skipped."""
    pending: dict[str, tuple[str, dict]] = {}
    cwd = ""
    for rec in _iter_jsonl(path):
        if kind == "claude":
            cwd = cwd or str(rec.get("cwd") or "")
            if rec.get("isSidechain"):
                continue
            msg = rec.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                    pending[str(block.get("id") or "")] = (str(block.get("name") or ""), inp)
                elif block.get("type") == "tool_result":
                    call = pending.pop(str(block.get("tool_use_id") or ""), None)
                    text = _claude_result_text(block.get("content"))
                    if call and text:
                        yield ToolOutput("claude", call[0], call[1], text, bool(block.get("is_error")), cwd)
        else:
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            ptype = payload.get("type")
            if rec.get("type") == "session_meta":
                cwd = str(payload.get("cwd") or "")
            if rec.get("type") != "response_item":
                continue
            if ptype in ("function_call", "custom_tool_call"):
                pending[str(payload.get("call_id") or "")] = (str(payload.get("name") or ""), _codex_args(payload))
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                call = pending.pop(str(payload.get("call_id") or ""), None)
                text = _codex_output_text(payload.get("output"))
                if call and text:
                    yield ToolOutput("codex", call[0], call[1], text, False, cwd)


def _category(out: ToolOutput) -> str:
    low = out.name.lower()
    if "terser" in low or _has_terser_marker(out.text):
        return "terser"
    if out.name in _SHELL_TOOLS or low in ("shell", "exec_command", "local_shell"):
        return "shell"
    if out.name in _READ_TOOLS:
        return "read"
    if low.startswith("mcp__") or (out.host == "codex" and "__" in low):
        return "mcp"
    return "other"


def _shell_cmd(inp: dict) -> str:
    cmd = inp.get("command") or inp.get("cmd") or ""
    return " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)


def _replay_one(out: ToolOutput, cat: str, cfg: dict[str, Any], env: dict[str, str]) -> str | None:
    """Return what Terser would have served, or None when it would have passed the output through."""
    from runtime import output_compressor

    text = out.text
    if cat == "shell":
        if len(text) <= _SHELL_MIN_CHARS or not cfg.get("bash_compress", True):
            return None
        comp, _ = output_compressor.compress_output(
            text, command=_shell_cmd(out.inp), returncode=1 if out.is_error else 0,
            enhanced=bool(cfg.get("enhanced_compression", True)), host="replay",
            workspace=out.cwd or None, env=env,
        )
        return comp
    if cat == "mcp":
        threshold = int(cfg.get("archive_threshold_bytes", 16384))
        if cfg.get("archive_large_outputs", True) and len(text) > threshold:
            comp, _ = output_compressor.compress_output(
                text, command=out.name, max_chars=threshold // 2,
                enhanced=bool(cfg.get("enhanced_compression", True)), host="replay",
                workspace=out.cwd or None, env=env,
            )
            return comp
        if cfg.get("enhanced_compression", True) and len(text) > _MCP_LOSSLESS_MIN_CHARS:
            comp, _ = output_compressor.compress_output(
                text, command=out.name, enhanced=True, lossless_only=True, host="replay",
                workspace=out.cwd or None, env=env,
            )
            return comp
        return None
    if cat == "read":
        if out.is_error or not cfg.get("outline_first", True) or out.inp.get("offset") or out.inp.get("limit"):
            return None
        from runtime import read_funnel

        fp = str(out.inp.get("file_path") or out.inp.get("path") or "")
        content = _READ_LINE_PREFIX.sub("", text)
        lock_re = getattr(read_funnel, "_LOCKFILE_NAME_RE", None)
        is_lock = bool(lock_re and lock_re.search(Path(fp).name))
        min_lines = 1 if is_lock else int(cfg.get("outline_min_lines", 250))
        if content.count("\n") + 1 < min_lines or len(content.encode("utf-8", "replace")) < int(cfg.get("outline_min_bytes", 10240)):
            return None
        outline = read_funnel.build_code_outline(fp, content, env=env)
        if not outline:
            return None
        # Same framing the read funnel adds around the outline.
        return f"[Terser] `{Path(fp).name}` is N lines. Outline (exact line spans):\n{outline}\n\nRead only the needed span with offset/limit, or retry this read immediately to load the full requested span."
    return None


def replay(
    host: str,
    days: float = 7.0,
    *,
    limit: int = 200,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run recorded tool outputs back through Terser's compressors. Read-only for Terser state.

    Compressors that archive or log (FTS sandbox, savings.jsonl, outline cache) are pointed
    at a throwaway ``TERSER_HOME`` that is deleted afterwards; the real config is copied in
    so thresholds match what the live hooks would use.
    """
    real_env = env if env is not None else dict(os.environ)
    cfg = load_config(real_env)
    files = _recent_files(host, days, limit, real_env)
    stats = {c: {"outputs": 0, "tok_in": 0, "avoided": 0, "compressed": 0} for c in REPLAY_CATEGORIES}
    tmp = tempfile.mkdtemp(prefix="terser-replay-")
    sandbox = dict(real_env, TERSER_HOME=tmp)
    try:
        real_cfg = state_home(real_env) / "config.json"
        if real_cfg.is_file():
            shutil.copyfile(real_cfg, Path(tmp) / "config.json")
    except OSError:
        pass
    prev_home = os.environ.get("TERSER_HOME")
    os.environ["TERSER_HOME"] = tmp  # covers any compressor path that ignores the env argument
    try:
        for _, kind, p in files:
            for out in iter_tool_outputs(p, kind):
                cat = _category(out)
                s = stats[cat]
                tok = estimate_tokens(out.text)
                s["outputs"] += 1
                s["tok_in"] += tok
                if cat in ("terser", "other"):
                    continue
                try:
                    served = _replay_one(out, cat, cfg, sandbox)
                except Exception:
                    served = None
                if served is not None and len(served) < int(len(out.text) * _KEEP_RATIO):
                    s["avoided"] += max(0, tok - estimate_tokens(served))
                    s["compressed"] += 1
    finally:
        if prev_home is None:
            os.environ.pop("TERSER_HOME", None)
        else:
            os.environ["TERSER_HOME"] = prev_home
        shutil.rmtree(tmp, ignore_errors=True)
    funnel = load_funnel_summary(real_env).get("t2") or {}
    t2_served, t2_retry = int(funnel.get("served") or 0), int(funnel.get("retry") or 0)
    retry_rate = min(1.0, t2_retry / t2_served) if t2_served else 0.0
    applied = t2_served >= _MIN_RETRY_SAMPLE
    stats["read"]["avoided_gross"] = stats["read"]["avoided"]
    if applied:
        stats["read"]["avoided"] = int(stats["read"]["avoided"] * (1.0 - retry_rate))
    return {
        "host": host,
        "days": days,
        "sessions": len(files),
        "categories": stats,
        "outline_retry_rate": retry_rate,
        "outline_samples": t2_served,
        "outline_retry_applied": applied,
    }


def format_replay(res: dict[str, Any], *, price_in: float | None = None, reread_turns: float = DEFAULT_REREAD_TURNS) -> str:
    price = pricing(price_in=price_in)
    cats = res["categories"]
    lines = [
        f"Terser discover --replay — last {res['days']:g} day(s), host={res['host']}, {res['sessions']} transcript(s)",
        "  Counterfactual: recorded tool outputs re-run through Terser's own compressors (read-only).",
        "",
        "  Category             Outputs  Compressed   Tokens in   Would avoid     %",
    ]
    tot_in = tot_av = 0
    for c in REPLAY_CATEGORIES:
        s = cats[c]
        if not s["outputs"]:
            continue
        tot_in += s["tok_in"]
        tot_av += s["avoided"]
        pct = f"{100 * s['avoided'] / s['tok_in']:4.0f}%" if s["tok_in"] and c not in ("terser", "other") else "    -"
        avoid = _fmt_tok(s["avoided"]) if c not in ("terser", "other") else "-"
        comp = str(s["compressed"]) if c not in ("terser", "other") else "-"
        lines.append(f"  {_REPLAY_LABELS[c]:<20} {s['outputs']:>7}  {comp:>10}  {_fmt_tok(s['tok_in']):>10}  {avoid:>12}  {pct}")
    if not tot_in:
        lines.append("  No tool outputs found in the window.")
    else:
        pct = 100 * tot_av / tot_in
        lines.append(f"  {'Total':<20} {'':>7}  {'':>10}  {_fmt_tok(tot_in):>10}  {_fmt_tok(tot_av):>12}  {pct:4.0f}%")
        lines.append("")
        lines.append(
            f"  Estimated value: ~${saved_cost(tot_av, price, reread_turns):.2f} at cached rates "
            f"(~${tot_av * price['price_input_per_mtok'] / 1e6:.2f} if each token were billed once at full input price, no re-reads)."
        )
    rr = res.get("outline_retry_rate", 0.0)
    gross = cats["read"].get("avoided_gross", cats["read"]["avoided"])
    lines.append("")
    lines.append("  Honest accounting:")
    n = int(res.get("outline_samples") or 0)
    if res.get("outline_retry_applied"):
        lines.append(f"  - File-read savings discounted by your measured outline retry rate ({rr:.0%} of {n}); gross {_fmt_tok(gross)}.")
    elif n:
        lines.append(
            f"  - File reads are an upper bound: only {n} outline(s) measured so far, {rr:.0%} retried for the full file "
            f"(discount applies from {_MIN_RETRY_SAMPLE})."
        )
    else:
        lines.append("  - File reads assume no retry after an outline (no funnel data yet); treat them as an upper bound.")
    lines.append("  - 'Already via Terser' outputs are counted in savings.jsonl, not here; command deltas and the read cache are not simulated.")
    lines.append("  - Tokens are estimated at ~4 chars/token; nothing was written to Terser's state.")
    lines.append("  - " + _assumptions(price, reread_turns))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

_DASH_CSS = """
:root{--bg:#fbfaf7;--fg:#1f2328;--muted:#656d76;--card:#fff;--line:#e6e2da;--bar:#0f766e}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e6edf3;--muted:#8b949e;--card:#161b22;--line:#30363d;--bar:#2dd4bf}}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}
main{max-width:960px;margin:0 auto;padding:24px 16px}h1{font-size:22px;margin:0 0 4px}
.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.big{font-size:24px;font-weight:600;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left}
.bar{height:8px;background:var(--bar);border-radius:4px}.wrap{overflow-x:auto}
"""


def dashboard_html(
    host: str,
    days: float = 30.0,
    env: dict[str, str] | None = None,
    *,
    price_in: float | None = None,
    reread_turns: float = DEFAULT_REREAD_TURNS,
) -> str:
    price = pricing(price_in=price_in)
    since = time.time() - days * 86400
    savings = _load_jsonl("savings.jsonl", since, env)
    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_day: dict[str, int] = defaultdict(int)
    for r in savings:
        saved = int(r.get("saved_tok") or 0)
        by_cat[str(r.get("cat") or "other")][0] += saved
        by_cat[str(r.get("cat") or "other")][1] += 1
        by_day[time.strftime("%Y-%m-%d", time.localtime(float(r.get("ts") or 0)))] += saved
    total = sum(v[0] for v in by_cat.values())
    sessions = recent_sessions(host, min(days, 14), env=env) if host in ("claude", "codex", "all") else []
    grades = [quality_grade(s) for s in sessions]
    avg = round(sum(g[1] for g in grades) / len(grades)) if grades else None
    served, retried = funnel_retry_ratio(since, env)
    e = html.escape

    def cards() -> str:
        items = [
            ("Tokens kept out", _fmt_tok(total)),
            ("Value at cached rates", f"${saved_cost(total, price, reread_turns):.2f}"),
            ("Avg session quality", f"{avg}/100" if avg is not None else "n/a"),
            ("Read retries", f"{retried}/{served}" if served else "n/a"),
        ]
        return "".join(f'<div class="card"><div class="muted">{e(k)}</div><div class="big">{e(v)}</div></div>' for k, v in items)

    top = max((v[0] for v in by_cat.values()), default=1) or 1
    cat_rows = "".join(
        f"<tr><td>{e(c)}</td><td>{v[1]}</td><td>{_fmt_tok(v[0])}</td><td style='width:40%'><div class='bar' style='width:{100 * v[0] / top:.1f}%'></div></td></tr>"
        for c, v in sorted(by_cat.items(), key=lambda kv: -kv[1][0])
    ) or "<tr><td colspan=4 class='muted'>No savings recorded yet.</td></tr>"
    day_top = max(by_day.values(), default=1) or 1
    day_rows = "".join(
        f"<tr><td>{e(d)}</td><td>{_fmt_tok(n)}</td><td style='width:60%'><div class='bar' style='width:{100 * n / day_top:.1f}%'></div></td></tr>"
        for d, n in sorted(by_day.items())[-21:]
    )
    sess_rows = "".join(
        f"<tr><td>{e(time.strftime('%m-%d %H:%M', time.localtime(s.started or 0)))}</td><td>{e(Path(s.cwd).name or '-')}</td>"
        f"<td>{s.turns}</td><td>${session_cost(s, price):.2f}</td><td>{g[0]} {g[1]}</td><td>{e(g[2][0] if g[2] else '-')}</td></tr>"
        for s, g in zip(sessions[:15], grades[:15])
    ) or "<tr><td colspan=6 class='muted'>No transcripts for this host.</td></tr>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Terser Dashboard</title><style>{_DASH_CSS}</style></head><body><main>
<h1>Terser Dashboard</h1><div class="muted">Last {days:g} days · host {e(host)} · generated {e(time.strftime('%Y-%m-%d %H:%M'))}</div>
<div class="grid">{cards()}</div>
<div class="card wrap"><h3>Savings by layer</h3><table><tr><th>Layer</th><th>Events</th><th>Saved</th><th></th></tr>{cat_rows}</table></div><br>
<div class="card wrap"><h3>Savings by day</h3><table>{day_rows}</table></div><br>
<div class="card wrap"><h3>Recent sessions</h3><table><tr><th>Started</th><th>Project</th><th>Turns</th><th>Est. cost</th><th>Grade</th><th>Top issue</th></tr>{sess_rows}</table></div>
<p class="muted">{e(_assumptions(price, reread_turns))}</p></main></body></html>"""


def write_dashboard(
    host: str,
    *,
    open_browser: bool = True,
    env: dict[str, str] | None = None,
    price_in: float | None = None,
    reread_turns: float = DEFAULT_REREAD_TURNS,
) -> Path:
    out = state_home(env) / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dashboard_html(host, env=env, price_in=price_in, reread_turns=reread_turns), encoding="utf-8")
    if open_browser:
        try:
            webbrowser.open(out.as_uri())
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Compaction continuity
# ---------------------------------------------------------------------------

def continuity_path(workspace: str, env: dict[str, str] | None = None) -> Path:
    return state_home(env) / "continuity" / f"{workspace_hash(workspace) or 'global'}.md"


def compact_capture(payload: dict[str, Any], trigger: str = "stop", env: dict[str, str] | None = None) -> Path | None:
    """Write a small restore file for the workspace from the session transcript."""
    tp = payload.get("transcript_path")
    ws = normalize_workspace(str(payload.get("cwd") or ""))
    if not tp or not Path(str(tp)).is_file():
        return None
    st = parse_claude_transcript(Path(str(tp)))
    ws = ws or normalize_workspace(st.cwd)
    if not ws:
        return None
    lines = [f"Working dir: {ws}", f"Captured: {time.strftime('%Y-%m-%d %H:%M')} ({trigger})"]
    if st.prompts:
        lines.append("Recent requests: " + " | ".join(_short(p, 120) for p in st.prompts[-3:]))
    if st.edited:
        lines.append("Edited files: " + ", ".join(st.edited[-8:]))
    if st.errors:
        lines.append("Last errors: " + " | ".join(st.errors[-2:]))
    mode = _activity.classify_mode(st.tool_seq, errors=len(st.errors[-3:]))
    if mode != "general":
        lines.append(f"Activity: {mode}")
    if st.decisions:
        lines.append("Decisions: " + " | ".join(_short(d, 140) for d in st.decisions[-5:]))
    path = continuity_path(ws, env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        return None
    return path


def compact_instructions(payload: dict[str, Any]) -> str:
    """PreCompact guidance naming what the summary must keep."""
    tp = payload.get("transcript_path")
    if not tp or not Path(str(tp)).is_file():
        return ""
    st = parse_claude_transcript(Path(str(tp)))
    mode = _activity.classify_mode(st.tool_seq, errors=len(st.errors[-3:]))
    preserve, drop = _activity.mode_guidance(mode)
    parts = ["When summarizing, keep verbatim: the user's latest request and any unfinished steps"]
    if st.edited:
        parts.append("the files edited this session (" + ", ".join(Path(p).name for p in st.edited[-8:]) + ") and why")
    if st.errors:
        parts.append("the unresolved errors (" + " | ".join(_short(e, 80) for e in st.errors[-2:]) + ")")
    parts.append("decisions the user made. Drop tool output that can be re-fetched.")
    text = "; ".join(parts)
    text += f"\nCurrent activity: {mode}. PRESERVE {preserve}. DROP {drop}."
    if st.decisions:
        text += "\nCRITICAL DECISIONS (keep each verbatim):\n" + "\n".join(f"- {d}" for d in st.decisions[-_activity.MAX_DECISIONS:])
    return text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="terse-analytics")
    ap.add_argument("command", choices=["report", "coach", "dashboard", "compact-capture", "compact-instructions", "grade", "discover"])
    ap.add_argument("--host", default=os.environ.get("TERSER_HOST", "claude"))
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--trigger", default="stop")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--price-in", type=float, default=None)
    ap.add_argument("--reread-turns", type=float, default=DEFAULT_REREAD_TURNS)
    a, _ = ap.parse_known_args(argv)
    if a.command == "discover":
        res = replay(a.host, a.days, limit=a.limit)
        print(json.dumps(res, indent=2) if a.json else format_replay(res, price_in=a.price_in, reread_turns=a.reread_turns))
    elif a.command == "report":
        print(report(a.host, a.days, price_in=a.price_in, reread_turns=a.reread_turns))
    elif a.command == "coach":
        print(coach(a.host, a.days))
    elif a.command == "dashboard":
        p = write_dashboard(a.host, open_browser=not (a.quiet or a.no_open), price_in=a.price_in, reread_turns=a.reread_turns)
        if not a.quiet:
            print(f"Dashboard: {p}")
    elif a.command in ("compact-capture", "compact-instructions"):
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        if a.command == "compact-capture":
            compact_capture(payload, a.trigger)
        else:
            print(compact_instructions(payload))
    elif a.command == "grade":
        ss = recent_sessions(a.host, a.days, limit=1)
        letter, score, reasons = quality_grade(ss[0] if ss else None)
        print(f"{letter} ({score}/100)" + (": " + "; ".join(reasons) if reasons else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
