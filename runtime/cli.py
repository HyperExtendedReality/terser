#!/usr/bin/env python3
"""Terser — Unified CLI across Antigravity 2.0, Claude Code, and Codex.

Commands:
  status        Unified status, active optimizations, index status, and cumulative token savings
  savings       Breakdown of tokens saved by optimization layer (read funnel, output compress, etc.)
  audit         Run full session token & cost audit for the active host
  coach         Run project token-efficiency coaching
  dashboard     Generate/refresh local analytics dashboard
  expand <id>   Retrieve full raw output from ~/.terser/archive/<id>.txt
  config        Show or set Terser settings (show | set key=value ... | reset)
  microskills   Inspect/forget/unforget learned repo EnvFacts micro-skills
  toolprune     Review/apply/restore unused tool pruning
  doctor        Verify Python, bundled engines, AST index MCP, hooks, and state directory
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
    TO_VENDOR_DIR,
    TE_VENDOR_DIR,
    config_path,
    ensure_vendor_paths,
    load_config,
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


def _cmd_status(host: str, cwd: str) -> int:
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
        print(f"  Workspace idx : not indexed yet (PageRank repo_map active; or run `order index_folder`)")
    print(f"  Guards        : loop_guard={cfg['loop_guard']} mcp_refetch_guard={cfg['mcp_refetch_guard']} code_discipline={cfg['code_discipline']} toolprune={cfg['toolprune']}")
    # Cache-aware dollar estimate ($3/MTok fresh input, $0.30/MTok cached input -> ~$1.50/MTok blended)
    est_usd_fresh = (total_saved / 1_000_000) * 3.0
    est_usd_blended = (total_saved / 1_000_000) * 1.5
    print(f"  Local savings : {total_saved:,} tokens across {events} event(s) (~${est_usd_blended:.2f}-${est_usd_fresh:.2f} est. input savings)")
    if by_cat:
        for cat, info in sorted(by_cat.items(), key=lambda kv: -kv[1]["saved_tok"]):
            raw_tok = max(1, int(info.get("raw_tok") or 0))
            pct = min(99.9, (info["saved_tok"] / raw_tok) * 100.0)
            print(f"    - {cat:<16} {info['saved_tok']:>8,} tok saved ({pct:>4.1f}% across {info['events']} hit(s))")
    overrides = _load_outline_override_count()
    outline_hits = int((by_cat.get("read_outline") or {}).get("events") or 0)
    if outline_hits or overrides:
        print(f"  Outline stats : {outline_hits} outline(s) served, {overrides} full-file override(s)")
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


def _run_measure(host: str, measure_args: list[str]) -> int:
    measure_py = TO_VENDOR_DIR / "measure.py"
    if not measure_py.is_file():
        print("Terser analytics engine missing. Run scripts/bundle_vendors.py.", file=sys.stderr)
        return 1
    env = dict(os.environ, TOKEN_OPTIMIZER_RUNTIME=host, PYTHONUTF8="1")
    proc = subprocess.run([sys.executable, str(measure_py), *measure_args], env=env)
    return int(proc.returncode)


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
    fast_bin = _PLUGIN_ROOT / "bin" / ("terser-fast.exe" if os.name == "nt" else "terser-fast")
    if fast_bin.is_file():
        print(f"  [PASS] Rust fast-path  : {fast_bin} ({fast_bin.stat().st_size:,} B)")
    else:
        print("  [INFO] Rust fast-path  : not built (pure-Python fallback; run `python scripts/install.py --build-native`)")
    if TE_VENDOR_DIR.is_dir():
        print(f"  [PASS] Micro-skills    : bundled at {TE_VENDOR_DIR}")
    else:
        print(f"  [FAIL] Micro-skills    : missing at {TE_VENDOR_DIR}")
        ok = False
    if (TO_VENDOR_DIR / "measure.py").is_file():
        print(f"  [PASS] Analytics       : bundled at {TO_VENDOR_DIR}")
    else:
        print(f"  [FAIL] Analytics       : missing at {TO_VENDOR_DIR}")
        ok = False
    idx_cmd = index_bridge.find_index_cmd()
    if idx_cmd:
        print(f"  [PASS] AST Index MCP   : {' '.join(idx_cmd)}")
    else:
        print("  [WARN] AST Index MCP   : AST code-index MCP binary / `uvx` not found on PATH")
    is_repo_root = (_PLUGIN_ROOT / ".claude-plugin").is_dir() or (_PLUGIN_ROOT / ".codex-plugin").is_dir()
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
        return _cmd_status(eff_host, cwd)
    if cmd == "config":
        return _cmd_config(sub)
    if cmd == "expand":
        if not sub:
            print("Usage: terse expand <archive-id|--last> [start_line end_line]", file=sys.stderr)
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
        bench_py = _PLUGIN_ROOT / "scripts" / "benchmark.py"
        if not bench_py.is_file():
            print("Benchmark script not found.", file=sys.stderr)
            return 1
        return int(subprocess.run([sys.executable, str(bench_py)]).returncode)
    if cmd == "audit-config":
        print(config_auditor.format_audit(config_auditor.audit_workspace(cwd)))
        return 0
    if cmd == "route":
        if not sub:
            print('Usage: terse route "<task description>"', file=sys.stderr)
            return 2
        print(task_router.format_route(task_router.classify_task(" ".join(sub))))
        return 0
    if cmd == "doctor":
        return _cmd_doctor(eff_host, cwd)
    if cmd == "audit":
        if eff_host == "antigravity":
            return _run_measure(eff_host, ["antigravity-summary", *sub])
        return _run_measure(eff_host, ["report", *sub])
    if cmd == "coach":
        return _run_measure(eff_host, ["coach", *sub])
    if cmd == "dashboard":
        return _run_measure(eff_host, ["dashboard", "--user", *sub])
    if cmd in ("microskills", "toolprune", "repos"):
        return _run_te_cli(eff_host, cwd, [cmd, *sub])

    print(__doc__)
    return 0 if cmd in ("-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main())
