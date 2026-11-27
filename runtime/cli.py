#!/usr/bin/env python3
"""Terser — Unified CLI across Antigravity 2.0, Claude Code, and Codex.

Commands:
  status        Unified status, active optimizations, index status, and cumulative token savings
  savings       Breakdown of tokens saved by optimization layer (read funnel, output compress, etc.)
  audit         Run full session token & cost audit for the active host (with regret signals)
  discover      --replay [--days N] [--json]: replay transcripts through Terser's compressors (read-only)
  coach         Run project token-efficiency coaching
  grade         Quality grade (0-100) for the latest session
  statusline    One-line status for Claude Code's statusLine setting (reads stdin JSON)
  mcp-proxy -- <cmd>  Wrap a stdio MCP server and compress what it returns
  security-report  Secret-leak and config audit for this workspace
  dashboard     Generate/refresh local analytics dashboard
  expand <id>   Retrieve full raw output from ~/.terser/archive/<id>.txt
  exec          Run explicit non-interactive argv with Codex command deltas
  config        Show or set Terser settings (show | set key=value ... | reset)
  microskills   Inspect/forget/unforget learned repo EnvFacts micro-skills
  toolprune     Review/apply/restore unused tool pruning
  doctor        Verify Python, bundled engines, AST index MCP, hooks, and state directory
  resume-lean   Token-free restart context for this workspace (checkpoint, events, git state)
  filters [test <cmd>]  Show command-filters.toml / .contextignore files and test a command
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _THIS_DIR.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from runtime import (
    __version__,
    index_bridge,
    config_auditor,
    context_sandbox,
    output_compressor,
    repo_map,
    task_router,
)
from runtime.config import (
    DEFAULT_CONFIG,
    TE_VENDOR_DIR,
    config_path,
    ensure_vendor_paths,
    load_config,
    load_funnel_summary,
    save_config,
    state_home,
)


def _detect_cli_host(explicit: str | None) -> str:
    if explicit in ("antigravity", "claude", "codex"):
        return explicit
    if os.environ.get("ANTIGRAVITY_AGENT") == "1" or os.environ.get("ANTIGRAVITY_CONVERSATION_ID"):
        return "antigravity"
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_HOME"):
        return "codex"
    if os.environ.get("CLAUDECODE") == "1" or os.environ.get("CLAUDE_CODE_SESSION_ID"):
        return "claude"
    return "antigravity"


def _load_savings_summary() -> tuple[int, int, dict[str, dict[str, int]]]:
    log = state_home() / "savings.jsonl"
    by_cat: dict[str, dict[str, int]] = defaultdict(lambda: {"events": 0, "raw_tok": 0, "saved_tok": 0})
    total_events = 0
    total_saved = 0
    if not log.is_file():
        return 0, 0, {}
    try:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            cat = str(rec.get("cat") or "other")
            saved = int(rec.get("saved_tok") or 0)
            raw = int(rec.get("raw_tok") or 0)
            by_cat[cat]["events"] += 1
            by_cat[cat]["raw_tok"] += raw
            by_cat[cat]["saved_tok"] += saved
            total_events += 1
            total_saved += saved
    except OSError:
        pass
    return total_events, total_saved, dict(by_cat)


def _load_outline_override_count() -> int:
    cdir = state_home() / "read-cache"
    total = 0
    try:
        if cdir.is_dir():
            for p in cdir.glob("*.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    meta = data.get("meta") if isinstance(data, dict) else None
                    if isinstance(meta, dict):
                        total += int(meta.get("outline_overrides") or 0)
                except (OSError, ValueError):
                    continue
    except OSError:
        pass
    return total


def _cmd_status(host: str, cwd: str, sub: list[str] | None = None) -> int:
    sub = sub or []
    cfg = load_config()
    events, total_saved, by_cat = _load_savings_summary()
    idx = index_bridge.workspace_index_summary(cwd)
    idx_cmd = index_bridge.find_index_cmd()

    print(f"Terser v{__version__}  (host={host}, enabled={cfg['enabled']}, shadow={cfg['shadow']})")
    print(f"  State dir     : {state_home()}")
    print(f"  Read funnel   : cache={cfg['read_cache']} delta={cfg['delta_diff']} outline_first={cfg['outline_first']} (>= {cfg['outline_min_lines']}L, backoff={cfg['outline_backoff_limit']})")
    print(f"  Shell pipeline: form_repair={cfg['form_repair']} compress={cfg['bash_compress']} cmd_delta={cfg['cmd_delta']} fts_sandbox={cfg['fts_sandbox']}")
    print(f"  Code index    : surface={cfg['index_surface']} auto_reindex={cfg['index_auto_reindex']} repo_map={cfg['repo_map']} binary={' '.join(idx_cmd) if idx_cmd else 'missing'}")
    if idx:
        print(f"  Workspace idx : {idx['repo']} ({idx['files']} files, {idx['symbols']} symbols)")
    else:
        print(f"  Workspace idx : not indexed yet (PageRank repo_map active; git repos auto-index in background at session start)")
    print(f"  Guards        : loop_guard={cfg['loop_guard']} mcp_refetch_guard={cfg['mcp_refetch_guard']} code_discipline={cfg['code_discipline']} toolprune={cfg['toolprune']}")
    from runtime import analytics

    price_in, reread = _price_args(sub)
    price = analytics.pricing(cfg, price_in=price_in)
    print(f"  Local savings : {total_saved:,} tokens across {events} event(s) (~${analytics.saved_cost(total_saved, price, reread):.2f} at cached input rates)")
    print("  " + analytics._assumptions(price, reread).replace("Assumptions:", "Value model   :", 1))
    try:
        latest = analytics.recent_sessions(host, days=2, limit=1) if host in ("claude", "codex") else []
        letter, score, reasons = analytics.quality_grade(latest[0] if latest else None)
        if latest:
            print(f"  Session grade : {letter} ({score}/100){': ' + '; '.join(reasons) if reasons else ''}")
            for ln in analytics.regret_lines(latest, indent="  "):
                print(ln)
    except Exception:
        pass
    if by_cat:
        for cat, info in sorted(by_cat.items(), key=lambda kv: -kv[1]["saved_tok"]):
            raw_tok = max(1, int(info.get("raw_tok") or 0))
            pct = min(99.9, (info["saved_tok"] / raw_tok) * 100.0)
            print(f"    - {cat:<16} {info['saved_tok']:>8,} tok saved ({pct:>4.1f}% across {info['events']} hit(s))")
    overrides = _load_outline_override_count()
    outline_hits = int((by_cat.get("read_outline") or {}).get("events") or 0)
    if outline_hits or overrides:
        print(f"  Outline stats : {outline_hits} outline(s) served, {overrides} full-file override(s)")
    funnel = load_funnel_summary()
    if funnel:
        parts = []
        for tier, c in sorted(funnel.items()):
            served = int(c.get("served") or 0)
            retry = int(c.get("retry") or 0)
            if served:
                parts.append(f"{tier} {retry}/{served} ({retry / served * 100:.0f}%)")
            elif retry:
                parts.append(f"{tier} {retry} retr{'y' if retry == 1 else 'ies'}")
            mismatch = int(c.get("mismatch") or 0)
            if mismatch:
                parts.append(f"{tier} {mismatch} disk mismatch(es) caught")
        if parts:
            print("  Funnel retries: " + ", ".join(parts) + "  (retried / short answers served)")
    return 0


def _cmd_routing(args: list[str]) -> int:
    """`terse routing [status | on | off | verify on|off | model <name> | effort <level> | check "<prompt>" [type]]`."""
    from runtime import subagent_verify as sv

    cfg = load_config()
    host_override = ""
    clean_args: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            host_override = args[i + 1].strip().lower()
            i += 2
        else:
            clean_args.append(args[i])
            i += 1
    args = clean_args
    op = args[0].lower() if args else "status"
    if op in ("on", "off"):
        cfg["subagent_model_routing"] = op == "on"
        print(f"Low-cost subagent routing {'enabled' if op == 'on' else 'disabled'} ({save_config(cfg)}).")
        return 0
    if op == "verify" and len(args) >= 2 and args[1].lower() in ("on", "off"):
        cfg["subagent_verify"] = args[1].lower() == "on"
        print(f"Double check {'enabled' if cfg['subagent_verify'] else 'disabled'} ({save_config(cfg)}).")
        return 0
    if op == "model" and len(args) >= 2:
        val = args[1].strip()
        low = val.lower()
        is_ag = host_override == "antigravity" or (not host_override and low in sv.AGY_SUBAGENT_MODELS)
        if is_ag:
            cfg["subagent_cheap_model_antigravity"] = low if low in sv.AGY_SUBAGENT_MODELS else val
            print(f"Antigravity 2.0 low-cost subagent model set to `{cfg['subagent_cheap_model_antigravity']}` ({save_config(cfg)}).")
        else:
            cfg["subagent_cheap_model"] = val
            print(f"Claude Code low-cost tier set to `{val}` ({save_config(cfg)}).")
        return 0
    if op == "effort" and len(args) >= 2:
        level = args[1].strip().lower()
        if level not in sv.EFFORT_LEVELS + ("inherit",):
            print(f"Effort must be one of: {', '.join(sv.EFFORT_LEVELS)}, inherit", file=sys.stderr)
            return 2
        stored = "" if level == "inherit" else level
        if host_override == "antigravity":
            cfg["subagent_cheap_effort_antigravity"] = stored
            print(f"Antigravity 2.0 routed subagent effort set to `{level}` ({save_config(cfg)}).")
        elif host_override == "claude":
            cfg["subagent_cheap_effort"] = stored
            print(f"Claude Code routed subagent effort set to `{level}` ({save_config(cfg)}).")
        else:
            cfg["subagent_cheap_effort"] = stored
            cfg["subagent_cheap_effort_antigravity"] = stored
            print(f"Routed subagent effort set to `{level}` for Antigravity 2.0 & Claude Code ({save_config(cfg)}).")
        return 0
    if op == "check" and len(args) >= 2:
        ok, why = sv.should_route(args[1], args[2] if len(args) > 2 else "research", "")
        print(f"{'ROUTE to low-cost tier' if ok else 'KEEP parent model'}: {why}")
        return 0
    if op != "status":
        print(
            'Usage: terse routing [status | on | off | verify on|off | model <name> | effort <level> | check "<prompt>" [type]] [--host antigravity|claude]',
            file=sys.stderr,
        )
        return 2
    st = sv.stats()
    print(f"Low-cost subagent routing: {'ON' if cfg.get('subagent_model_routing') else 'OFF'}"
          f"  (double check={'ON' if cfg.get('subagent_verify', True) else 'OFF'})")
    print(f"  Antigravity 2.0: model={cfg.get('subagent_cheap_model_antigravity', 'flash')} (available: {', '.join(sv.AGY_SUBAGENT_MODELS)}),"
          f" effort={cfg.get('subagent_cheap_effort_antigravity') or 'inherit'} (available: {', '.join(sv.AGY_EFFORT_LEVELS)}, inherit)")
    print(f"  Claude Code    : model={cfg.get('subagent_cheap_model', 'haiku')}, effort={cfg.get('subagent_cheap_effort') or 'inherit'}")
    print(f"  Last 7 days: {st['routed']} routed, {st['checked']} checked, {st['failed']} failed the double check"
          + ("  -> AUTO BACKOFF active (routing paused)" if st["backed_off"] else ""))
    print("  Routes only read-only lookups (Explore/research, or general-purpose with no write intent)")
    print("  when the caller set no model; never security, concurrency, migration or long briefs.")
    return 0


def _cmd_config(args: list[str]) -> int:
    cfg = load_config()
    if not args or args[0] == "show":
        print(json.dumps(cfg, indent=2, sort_keys=True))
        return 0
    if args[0] == "reset":
        p = save_config(dict(DEFAULT_CONFIG))
        print(f"Reset config to defaults at {p}")
        return 0
    if args[0] == "set" and len(args) >= 2:
        for item in args[1:]:
            if "=" not in item:
                print(f"Invalid pair (expected key=value): {item}", file=sys.stderr)
                return 2
            k, v = item.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k not in DEFAULT_CONFIG:
                print(f"Unknown setting: {k}. Valid keys: {', '.join(sorted(DEFAULT_CONFIG))}", file=sys.stderr)
                return 2
            default_val = DEFAULT_CONFIG[k]
            if isinstance(default_val, bool):
                cfg[k] = v.lower() in ("1", "true", "yes", "on")
            elif isinstance(default_val, int):
                try:
                    cfg[k] = int(v)
                except ValueError:
                    print(f"Setting {k} requires an integer, got: {v}", file=sys.stderr)
                    return 2
            else:
                cfg[k] = v
        p = save_config(cfg)
        print(f"Updated {p}")
        return 0
    print("Usage: terse config [show | set key=value ... | reset]", file=sys.stderr)
    return 2


def _float_arg(sub: list[str], names: tuple[str, ...], default: float | None) -> float | None:
    for i, a in enumerate(sub):
        if a in names and i + 1 < len(sub):
            try:
                return float(sub[i + 1])
            except ValueError:
                break
        for n in names:
            if n.startswith("--") and a.startswith(n + "="):
                try:
                    return float(a.split("=", 1)[1])
                except ValueError:
                    return default
    return default


def _days_arg(sub: list[str], default: float) -> float:
    val = _float_arg(sub, ("--days", "-d"), default)
    return default if val is None else val


def _price_args(sub: list[str]) -> tuple[float | None, float]:
    """``--price-in <$/MTok>`` and ``--reread-turns <N>`` for the savings value model."""
    from runtime import analytics

    price_in = _float_arg(sub, ("--price-in",), None)
    reread = _float_arg(sub, ("--reread-turns",), analytics.DEFAULT_REREAD_TURNS)
    return price_in, max(0.0, reread if reread is not None else analytics.DEFAULT_REREAD_TURNS)


def _run_te_cli(host: str, cwd: str, te_args: list[str]) -> int:
    if not TE_VENDOR_DIR.is_dir():
        print("Terser micro-skills engine missing. Run scripts/bundle_vendors.py.", file=sys.stderr)
        return 1
    ensure_vendor_paths()
    try:
        from tokeneconomizer.plugin import cli as te_cli  # type: ignore

        return int(te_cli.main(["--host", host, "--cwd", cwd, *te_args]) or 0)
    except Exception as exc:
        print(f"Terser micro-skills CLI error: {exc}", file=sys.stderr)
        return 1


def _cmd_doctor(host: str, cwd: str) -> int:
    ok = True
    print(f"Terser Doctor (v{__version__}, host={host})")
    print(f"  [PASS] Python          : {sys.version.split()[0]} ({sys.executable})")
    bin_name = "terser-codex" if host == "codex" else "terser-fast"
    staged = _PLUGIN_ROOT / "dist" / "codex" / "terser"
    runtime_root = staged if host == "codex" and (staged / "codex-mcp.json").is_file() else _PLUGIN_ROOT
    fast_bin = runtime_root / "bin" / (bin_name + (".exe" if os.name == "nt" else ""))
    if fast_bin.is_file():
        print(f"  [PASS] Rust fast-path  : {fast_bin} ({fast_bin.stat().st_size:,} B)")
    else:
        print("  [INFO] Rust fast-path  : not built (pure-Python fallback; run `python scripts/install.py --build-native`)")
    if TE_VENDOR_DIR.is_dir():
        print(f"  [PASS] Micro-skills    : bundled at {TE_VENDOR_DIR}")
    else:
        print(f"  [FAIL] Micro-skills    : missing at {TE_VENDOR_DIR}")
        ok = False
    if (_THIS_DIR / "analytics.py").is_file():
        print("  [PASS] Analytics       : native (runtime/analytics.py)")
    else:
        print("  [FAIL] Analytics       : runtime/analytics.py is missing")
        ok = False
    if host == "codex":
        from runtime import codex_doctor
        ok = codex_doctor.run(runtime_root, cwd) and ok
    else:
        idx_cmd = index_bridge.find_index_cmd()
        if idx_cmd:
            print(f"  [PASS] AST Index MCP   : {' '.join(idx_cmd)}")
        else:
            print("  [WARN] AST Index MCP   : AST code-index MCP binary / `uvx` not found on PATH")
    is_repo_root = (_PLUGIN_ROOT / "scripts" / "bundle_vendors.py").is_file() and (_PLUGIN_ROOT / ".git").exists()
    checks: list[tuple[str, Path, bool]] = [
        ("Antigravity manifest", _PLUGIN_ROOT / "plugin.json", host == "antigravity" or is_repo_root),
        ("Antigravity hooks", _PLUGIN_ROOT / "hooks.json", host == "antigravity" or is_repo_root),
        ("Antigravity MCP", _PLUGIN_ROOT / "mcp_config.json", host == "antigravity" or is_repo_root),
        ("Claude manifest", _PLUGIN_ROOT / ".claude-plugin" / "plugin.json", host == "claude" or is_repo_root),
        ("Claude hooks", _PLUGIN_ROOT / "hooks" / "hooks.json", host == "claude" or is_repo_root),
        ("Codex manifest", _PLUGIN_ROOT / ".codex-plugin" / "plugin.json", host == "codex" or is_repo_root),
        ("Codex hooks", _PLUGIN_ROOT / "hooks" / "codex-hooks.json", host == "codex" or is_repo_root),
    ]
    for label, p, required in checks:
        if not required and not p.is_file():
            continue
        try:
            json.loads(p.read_text(encoding="utf-8"))
            print(f"  [PASS] {label:<20}: {p.relative_to(_PLUGIN_ROOT)}")
        except Exception as exc:
            print(f"  [FAIL] {label:<20}: {exc}")
            ok = False
    print(f"  [INFO] Config file     : {config_path()} ({'exists' if config_path().is_file() else 'defaults'})")
    idx = index_bridge.workspace_index_summary(cwd)
    print(f"  [INFO] CWD code index  : {idx['repo'] if idx else 'not indexed'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["exec"]:
        return output_compressor.run_codex_exec(args[1:])
    host: str | None = None
    cwd = os.getcwd()
    rest: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--host" and i + 1 < len(args):
            host = args[i + 1]
            i += 2
        elif args[i] == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]
            i += 2
        else:
            rest.append(args[i])
            i += 1

    eff_host = _detect_cli_host(host)
    cmd = rest[0] if rest else "status"
    sub = rest[1:]

    if cmd in ("status", "savings"):
        return _cmd_status(eff_host, cwd, sub)
    if cmd == "config":
        return _cmd_config(sub)
    if cmd == "expand":
        if not sub:
            print("Usage: terse expand <archive-id|--last> [start_line end_line | --raw]", file=sys.stderr)
            return 2
        target_id = sub[0]
        if target_id in ("--last", "last", "latest"):
            target_id = context_sandbox.get_latest_archive_id() or ""
            if not target_id:
                print(f"No archived outputs found in {state_home()}", file=sys.stderr)
                return 1
        s_ln = int(sub[1]) if len(sub) >= 2 and sub[1].isdigit() else None
        e_ln = int(sub[2]) if len(sub) >= 3 and sub[2].isdigit() else None
        text = output_compressor.read_archived_output(target_id, start_line=s_ln, end_line=e_ln)
        if text is None:
            print(f"Archive `{target_id}` not found in {state_home()}", file=sys.stderr)
            return 1
        if sub[1:] == ["--raw"]:
            sys.stdout.buffer.write(text.encode("utf-8"))
            return 0
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        return 0
    if cmd == "query":
        if len(sub) < 2:
            print('Usage: terse query <archive-id|--last> "<keywords>"', file=sys.stderr)
            return 2
        res = context_sandbox.query_output(sub[0], " ".join(sub[1:]))
        if res is None:
            print(f"Archive `{sub[0]}` not found in sandbox.db", file=sys.stderr)
            return 1
        print(res)
        return 0
    if cmd == "repomap":
        budget = 400
        if len(sub) >= 2 and sub[0] == "--budget" and sub[1].isdigit():
            budget = int(sub[1])
        rmap = repo_map.build_repo_map(cwd, max_tokens=budget, min_files=1)
        if not rmap:
            print("No supported source files found to map.")
            return 0
        print(rmap)
        return 0
    if cmd == "benchmark":
        codex_bench = host == "codex" or "--codex" in sub
        bench_py = _PLUGIN_ROOT / "scripts" / ("benchmark_codex.py" if codex_bench else "benchmark.py")
        if not bench_py.is_file():
            print("Benchmark script not found.", file=sys.stderr)
            return 1
        return int(subprocess.run([sys.executable, str(bench_py), *(arg for arg in sub if arg != "--codex")]).returncode)
    if cmd == "audit-config":
        print(config_auditor.format_audit(config_auditor.audit_workspace(cwd)))
        return 0
    if cmd == "routing":
        return _cmd_routing(sub)
    if cmd == "route":
        if not sub:
            print('Usage: terse route "<task description>"', file=sys.stderr)
            return 2
        print(task_router.format_route(task_router.classify_task(" ".join(sub))))
        return 0
    if cmd == "hud":
        from runtime import hud

        return hud.main(sub + (["--cwd", cwd] if "--cwd" not in sub else []))
    if cmd == "statusline":
        from runtime import statusline

        return statusline.main()
    if cmd == "mcp-proxy":
        from runtime import mcp_proxy

        return mcp_proxy.main(sub)
    if cmd == "index-doc":
        if not sub:
            print("Usage: terse index-doc <file-path>", file=sys.stderr)
            return 2
        res = context_sandbox.index_file(sub[0])
        print(json.dumps(res, indent=2))
        return 0 if "archive_id" in res else 1
    if cmd == "security-report":
        print(config_auditor.format_audit(config_auditor.audit_workspace(cwd)))
        return 0
    if cmd == "doctor":
        return _cmd_doctor(eff_host, cwd)
    if cmd in ("resume-lean", "resume"):
        from runtime import activity

        text = activity.lean_resume(cwd)
        if not text:
            print("No checkpoint or logged events for this workspace yet.", file=sys.stderr)
            return 1
        print(text)
        return 0
    if cmd == "filters":
        from runtime import user_filters

        for path, scope in user_filters.filter_files(cwd):
            print(f"{scope:<8} {path} ({'present' if path.is_file() else 'absent'})")
        for path in user_filters.ignore_files(cwd):
            print(f"ignore   {path} ({'present' if path.is_file() else 'absent'})")
        loaded = user_filters.load_filters(cwd)
        for f in loaded["filters"]:
            print(f"  filter {f['name']!r} ({f['scope']}): match={f['match'].pattern!r} drop={len(f['drop'])} handler={f['handler'] or '-'}")
        for g in loaded["exclude"]:
            print(f"  exclude {g!r}")
        if sub[:1] == ["test"] and len(sub) >= 2:
            print("excluded" if user_filters.is_excluded(" ".join(sub[1:]), cwd) else
                  f"filter: {(user_filters.matching_filter(' '.join(sub[1:]), cwd) or {}).get('name', 'none')}")
        return 0
    if cmd == "discover":
        from runtime import analytics

        # Transcripts exist only for Claude Code and Codex; elsewhere replay both.
        replay_host = "all" if host == "all" or eff_host == "antigravity" else eff_host
        price_in, reread = _price_args(sub)
        limit = _float_arg(sub, ("--limit",), 200.0) or 200.0
        res = analytics.replay(replay_host, _days_arg(sub, 7.0), limit=int(limit))
        if "--json" in sub:
            print(json.dumps(res, indent=2))
        else:
            print(analytics.format_replay(res, price_in=price_in, reread_turns=reread))
        return 0
    if cmd in ("audit", "coach", "dashboard", "grade"):
        from runtime import analytics

        days = _days_arg(sub, 30.0 if cmd == "dashboard" else 7.0)
        price_in, reread = _price_args(sub)
        if cmd == "audit":
            print(analytics.report(eff_host, days, price_in=price_in, reread_turns=reread))
        elif cmd == "coach":
            print(analytics.coach(eff_host, days))
        elif cmd == "grade":
            return analytics.main(["grade", "--host", eff_host, "--days", str(days)])
        else:
            path = analytics.write_dashboard(
                eff_host, open_browser="--no-open" not in sub, price_in=price_in, reread_turns=reread,
            )
            print(f"Dashboard: {path}")
        return 0
    if cmd in ("microskills", "toolprune", "repos", "history"):
        return _run_te_cli(eff_host, cwd, [cmd, *sub])

    print(__doc__)
    return 0 if cmd in ("-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main())
