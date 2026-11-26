#!/usr/bin/env python3
"""Terser — Cross-Platform Installer & Bundle Builder.

Installs Terser cleanly and reversibly into:
  - Antigravity 2.0 / IDE / agy  (--antigravity) -> ~/.gemini/config/plugins/terser/
  - Claude Code                  (--claude)      -> builds self-contained bundle & prints/runs install
  - Codex                        (--codex)       -> installs hooks into ~/.codex/hooks.json & MCP into config.toml
  - All three                    (--all)
  - Standalone dist bundles      (--build-dist)  -> dist/antigravity/terser, dist/claude-codex/terser

Never touches unrelated user hooks or settings. Idempotent and reversible (--uninstall).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".DS_Store", "dist", "tests",
)
TERSER_MARKER = "terser"


def _resolve_python() -> str:
    return os.path.realpath(sys.executable)


def _antigravity_home() -> Path:
    raw = os.environ.get("TOKEN_OPTIMIZER_ANTIGRAVITY_HOME") or ""
    return Path(os.path.expanduser(raw)) if raw else (Path.home() / ".gemini")


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME") or ""
    return Path(os.path.expanduser(raw)) if raw else (Path.home() / ".codex")


_HOT_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart"})
_RELEASE_BASE = "https://github.com/HyperExtendedReality/terser/releases/latest/download"


def _native_bin_name() -> str:
    return "terser-fast.exe" if os.name == "nt" else "terser-fast"


def _native_bin_path(plugin_dir: Path) -> Path:
    return plugin_dir / "bin" / _native_bin_name()


def _release_asset_name() -> str | None:
    import platform

    arch = platform.machine().lower()
    if sys.platform == "win32" and arch in ("amd64", "x86_64"):
        return "terser-fast-windows-x86_64.exe"
    if sys.platform == "darwin" and arch in ("arm64", "aarch64"):
        return "terser-fast-macos-aarch64"
    if sys.platform.startswith("linux") and arch in ("x86_64", "amd64"):
        return "terser-fast-linux-x86_64"
    return None


def _download_native() -> Path | None:
    """Download prebuilt terser-fast binary from GitHub Releases (fail-open)."""
    import stat
    import urllib.request

    asset = _release_asset_name()
    if not asset:
        return None
    url = f"{_RELEASE_BASE}/{asset}"
    dest = _native_bin_path(PLUGIN_ROOT)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "terser-installer/1.2"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            data = resp.read()
        if len(data) < 32_768:
            return None
        tmp.write_bytes(data)
        if os.name != "nt":
            tmp.chmod(tmp.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp, dest)
        print(f"[Native] Downloaded prebuilt hot-path binary from Releases ({dest.stat().st_size:,} B)")
        return dest
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None


def build_native() -> Path | None:
    """Compile the Rust hot-path accelerator (terser-fast) into bin/."""
    import subprocess

    cargo = shutil.which("cargo")
    manifest = PLUGIN_ROOT / "native" / "terser-fast" / "Cargo.toml"
    if not cargo or not manifest.is_file():
        return None
    print(f"[Native] Building Rust hot-path accelerator ({manifest})...")
    proc = subprocess.run([cargo, "build", "--release", "--manifest-path", str(manifest)])
    if proc.returncode != 0:
        print("[Native] Cargo build failed — falling back to pure Python.")
        return None
    built = PLUGIN_ROOT / "native" / "terser-fast" / "target" / "release" / _native_bin_name()
    if not built.is_file():
        return None
    dest = _native_bin_path(PLUGIN_ROOT)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built, dest)
    print(f"  Installed native binary at {dest} ({dest.stat().st_size:,} B)")
    return dest


def ensure_native(*, force_rebuild: bool = False) -> Path | None:
    """Ensure bin/terser-fast is ready: reuse existing -> build with cargo -> download release -> Python fallback."""
    existing = _native_bin_path(PLUGIN_ROOT)
    if existing.is_file() and not force_rebuild:
        return existing
    built = build_native()
    if built:
        return built
    downloaded = _download_native()
    if downloaded:
        return downloaded
    print("[Native] Rust hot-path unavailable (no cargo / offline) — using pure-Python fallback.")
    return None


def _build_antigravity_hooks(plugin_dir: Path, python_exe: str) -> dict[str, Any]:
    hook_py = str(plugin_dir / "runtime" / "unified_hook.py")
    fast_bin = _native_bin_path(plugin_dir)
    use_native = fast_bin.is_file()

    def cmd(event: str) -> str:
        if use_native and event in _HOT_EVENTS:
            return f'"{fast_bin}" --python "{python_exe}" --script "{hook_py}" --host antigravity --event {event}'
        return f'"{python_exe}" -E -s -X utf8 "{hook_py}" --host antigravity --event {event}'

    return {
        "terser": {
            "SessionStart": [{"type": "command", "command": cmd("SessionStart"), "timeout": 10}],
            "PreInvocation": [{"type": "command", "command": cmd("PreInvocation"), "timeout": 10}],
            "PreToolUse": [{
                "matcher": "run_command|read_file|view_file|call_mcp_tool|mcp_.*",
                "hooks": [{"type": "command", "command": cmd("PreToolUse"), "timeout": 10}],
            }],
            "PostToolUse": [{
                "matcher": "run_command|read_file|view_file|edit_file|write_file|call_mcp_tool|mcp_.*",
                "hooks": [{"type": "command", "command": cmd("PostToolUse"), "timeout": 10}],
            }],
            "Stop": [{"type": "command", "command": cmd("Stop"), "timeout": 15}],
        }
    }


def _build_mcp_config(client_id: str, plugin_dir: Path | None = None) -> dict[str, Any]:
    root = plugin_dir or PLUGIN_ROOT
    index_mcp = str(root / "runtime" / "index_mcp.py")
    return {
        "mcpServers": {
            "terser-index": {
                "command": _resolve_python(),
                "args": ["-E", "-s", "-X", "utf8", index_mcp],
                "env": {
                    "TERSER_CLIENT_ID": client_id,
                    "TERSER_TOOL_SURFACE": "counter",
                },
            }
        }
    }


def _sync_tree(src: Path, dst: Path, *, ignore) -> None:
    """Copy `src` into `dst` cleanly, updating in place so Windows open directory
    handles (e.g. from a running Antigravity/MCP process) never trigger WinError 32."""
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)


def install_antigravity(*, dest_root: Path | None = None, dry_run: bool = False) -> Path:
    is_live_install = dest_root is None
    target = (dest_root / "terser") if dest_root else (_antigravity_home() / "config" / "plugins" / "terser")
    python_exe = _resolve_python()
    print(f"[Antigravity 2.0] Target plugin dir: {target}")
    if dry_run:
        print("  (dry-run) Would copy native Antigravity bundle, write OS-native hooks.json & mcp_config.json")
        return target

    # Copy only Antigravity-relevant files in place (safe on Windows even while Antigravity is running)
    _sync_tree(
        PLUGIN_ROOT,
        target,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".DS_Store",
            "dist", "tests", "native", ".claude-plugin", ".codex-plugin", ".mcp.json",
            "commands", "hooks",
        ),
    )
    if is_live_install:
        # Write platform-native direct-Python hooks.json (eliminates shell-fallback latency on Windows)
        hooks_doc = _build_antigravity_hooks(target, python_exe)
        (target / "hooks.json").write_text(json.dumps(hooks_doc, indent=2) + "\n", encoding="utf-8")
        # Write machine-resolved mcp_config.json with bundled terser-index Counter front door
        mcp_doc = _build_mcp_config("antigravity", plugin_dir=target)
        (target / "mcp_config.json").write_text(json.dumps(mcp_doc, indent=2) + "\n", encoding="utf-8")
        # Record Antigravity consent so bundled rollup works seamlessly
        to_cfg = _antigravity_home() / "token-optimizer" / "config.json"
        to_cfg.parent.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if to_cfg.is_file():
            try:
                existing = json.loads(to_cfg.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        existing["antigravity_consent"] = True
        to_cfg.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        print(f"  Installed Antigravity 2.0 plugin at {target}")
    else:
        print(f"  Built portable Antigravity 2.0 bundle at {target}")
    return target


def uninstall_antigravity(*, dry_run: bool = False) -> None:
    target = _antigravity_home() / "config" / "plugins" / "terser"
    if not target.exists():
        print(f"[Antigravity 2.0] Not installed at {target}")
        return
    if dry_run:
        print(f"[Antigravity 2.0] (dry-run) Would remove {target}")
        return
    shutil.rmtree(target)
    print(f"[Antigravity 2.0] Removed {target}")


def _codex_hook_command(plugin_dir: Path, event: str) -> str:
    py = _resolve_python()
    hook_py = str(plugin_dir / "runtime" / "unified_hook.py")
    fast_bin = _native_bin_path(plugin_dir)
    if fast_bin.is_file() and event in _HOT_EVENTS:
        return f'"{fast_bin}" --python "{py}" --script "{hook_py}" --host codex --event {event}'
    # Shell-neutral bare exe + quoted args works identically in cmd.exe, pwsh, and POSIX sh
    return f'{py} -E -s -X utf8 "{hook_py}" --host codex --event {event}'


_CODEX_BEGIN = "# BEGIN terser"
_CODEX_END = "# END terser"


def _render_codex_toml_block() -> str:
    mcp = _build_mcp_config("codex")["mcpServers"]["terser-index"]
    cmd = str(mcp["command"]).replace("\\", "\\\\")
    args_toml = ", ".join('"' + str(a).replace("\\", "\\\\") + '"' for a in mcp["args"])
    return (
        f"{_CODEX_BEGIN}\n"
        "[mcp_servers.terser-index]\n"
        f'command = "{cmd}"\n'
        f"args = [{args_toml}]\n"
        'env = { TERSER_CLIENT_ID = "codex", TERSER_TOOL_SURFACE = "counter" }\n'
        f"{_CODEX_END}\n"
    )


def _strip_codex_toml_block(text: str) -> str:
    out_lines: list[str] = []
    skipping = False
    for line in text.splitlines(keepends=True):
        s = line.strip()
        if s == _CODEX_BEGIN:
            skipping = True
            continue
        if skipping:
            if s == _CODEX_END:
                skipping = False
            continue
        out_lines.append(line)
    return "".join(out_lines)


def install_codex(*, dry_run: bool = False) -> Path:
    cx_home = _codex_home()
    hooks_path = cx_home / "hooks.json"
    config_toml = cx_home / "config.toml"
    print(f"[Codex] Target hooks file: {hooks_path}")

    existing: dict[str, Any] = {}
    if hooks_path.is_file():
        try:
            existing = json.loads(hooks_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    if not isinstance(existing, dict):
        existing = {}
    hooks = existing.get("hooks") if isinstance(existing.get("hooks"), dict) else {}

    events = [
        ("SessionStart", 15),
        ("SubagentStart", 10),
        ("UserPromptSubmit", 10),
        ("PreToolUse", 10),
        ("PostToolUse", 10),
        ("Stop", 10),
        ("SessionEnd", 3),
    ]
    for ev, timeout in events:
        entries = list(hooks.get(ev) or [])
        # Strip prior terser entries idempotently
        cleaned = [
            e for e in entries
            if TERSER_MARKER not in json.dumps(e)
        ]
        cleaned.append({
            "hooks": [{
                "type": "command",
                "command": _codex_hook_command(PLUGIN_ROOT, ev),
                "timeout": timeout,
            }]
        })
        hooks[ev] = cleaned
    existing["hooks"] = hooks

    if dry_run:
        print("  (dry-run) Would update ~/.codex/hooks.json and ~/.codex/config.toml (Counter MCP)")
        return hooks_path

    hooks_path.parent.mkdir(parents=True, exist_ok=True)
    hooks_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")

    # Idempotently register terser-index (Counter surface) in ~/.codex/config.toml
    cur_toml = config_toml.read_text(encoding="utf-8", errors="replace") if config_toml.is_file() else ""
    cleaned_toml = _strip_codex_toml_block(cur_toml).rstrip()
    new_toml = (cleaned_toml + "\n\n" if cleaned_toml else "") + _render_codex_toml_block()
    config_toml.write_text(new_toml, encoding="utf-8")

    print(f"  Installed Codex hooks in {hooks_path}")
    print(f"  Registered Counter MCP server in {config_toml}")
    print(f"  To also register the Codex plugin marketplace:")
    print(f'    codex plugin marketplace add "{PLUGIN_ROOT}"')
    print(f"    codex plugin add terser@terser")
    return hooks_path


def uninstall_codex(*, dry_run: bool = False) -> None:
    cx_home = _codex_home()
    hooks_path = cx_home / "hooks.json"
    config_toml = cx_home / "config.toml"
    if hooks_path.is_file():
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
            for ev in list(data["hooks"].keys()):
                if isinstance(data["hooks"][ev], list):
                    data["hooks"][ev] = [e for e in data["hooks"][ev] if TERSER_MARKER not in json.dumps(e)]
            if dry_run:
                print(f"[Codex] (dry-run) Would remove Terser hooks from {hooks_path}")
            else:
                hooks_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                print(f"[Codex] Removed Terser hooks from {hooks_path}")
    if config_toml.is_file():
        cur = config_toml.read_text(encoding="utf-8", errors="replace")
        if _CODEX_BEGIN in cur:
            if dry_run:
                print(f"[Codex] (dry-run) Would remove managed block from {config_toml}")
            else:
                config_toml.write_text(_strip_codex_toml_block(cur), encoding="utf-8")
                print(f"[Codex] Removed managed block from {config_toml}")


def build_dist(out_dir: Path | None = None) -> Path:
    dist = out_dir or (PLUGIN_ROOT / "dist")
    dist.mkdir(parents=True, exist_ok=True)

    # 1. Clean Antigravity 2.0 bundle
    ag_bundle = install_antigravity(dest_root=dist / "antigravity")
    # 2. Clean Claude Code + Codex marketplace bundle
    cc_bundle = dist / "claude-codex" / "terser"
    _sync_tree(
        PLUGIN_ROOT,
        cc_bundle,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".DS_Store",
            "dist", "tests", "plugin.json", "hooks.json", "mcp_config.json",
        ),
    )
    market_src = PLUGIN_ROOT / ".claude-plugin" / "marketplace.json"
    market = json.loads(market_src.read_text(encoding="utf-8"))
    for p in market.get("plugins", []):
        if p.get("name") == "terser":
            p["source"] = "./terser"
    market_out = dist / "claude-codex" / ".claude-plugin" / "marketplace.json"
    market_out.parent.mkdir(parents=True, exist_ok=True)
    market_out.write_text(json.dumps(market, indent=2) + "\n", encoding="utf-8")

    print(f"\nBuilt standalone bundles:")
    print(f"  Antigravity 2.0 : {ag_bundle}")
    print(f"  Claude & Codex  : {dist / 'claude-codex'}")
    return dist


def print_claude_instructions() -> None:
    print("[Claude Code] Install via local marketplace (recommended):")
    print(f'  claude plugin marketplace add "{PLUGIN_ROOT}"')
    print("  claude plugin install terser@terser")
    print("Or load directly for one session:")
    print(f'  claude --plugin-dir "{PLUGIN_ROOT}"')


def bump_version(part: str = "patch") -> str:
    """Bump semantic version across all 4 plugin manifests and runtime/__init__.py."""
    init_py = PLUGIN_ROOT / "runtime" / "__init__.py"
    ag_json = PLUGIN_ROOT / "plugin.json"
    cur = json.loads(ag_json.read_text(encoding="utf-8")).get("version", "1.0.0")
    major, minor, patch = (int(x) for x in cur.split(".")[:3])
    if part == "minor":
        minor += 1
        patch = 0
    else:
        patch += 1
    new_ver = f"{major}.{minor}.{patch}"

    for rel in ("plugin.json", ".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        p = PLUGIN_ROOT / rel
        doc = json.loads(p.read_text(encoding="utf-8"))
        doc["version"] = new_ver
        p.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    mkt = PLUGIN_ROOT / ".claude-plugin" / "marketplace.json"
    mkt_doc = json.loads(mkt.read_text(encoding="utf-8"))
    for entry in mkt_doc.get("plugins", []):
        if entry.get("name") == "terser":
            entry["version"] = new_ver
    mkt.write_text(json.dumps(mkt_doc, indent=2) + "\n", encoding="utf-8")

    init_text = init_py.read_text(encoding="utf-8")
    init_text = init_text.replace(f'__version__ = "{cur}"', f'__version__ = "{new_ver}"')
    init_py.write_text(init_text, encoding="utf-8")
    print(f"[Version] Bumped {cur} -> {new_ver} across all manifests")
    return new_ver


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--antigravity", action="store_true", help="Install/update Antigravity 2.0 (~/.gemini/config/plugins/terser)")
    ap.add_argument("--codex", action="store_true", help="Install/update Codex hooks (~/.codex/hooks.json) & Counter MCP (~/.codex/config.toml)")
    ap.add_argument("--claude", action="store_true", help="Print Claude Code plugin install/update commands")
    ap.add_argument("--all", action="store_true", help="Install/configure for Antigravity 2.0, Codex, and Claude Code")
    ap.add_argument("--build-dist", action="store_true", help="Build standalone dist/ bundles for all 3 platforms")
    ap.add_argument("--build-native", action="store_true", help="Force-rebuild the Rust hot-path accelerator (bin/terser-fast)")
    ap.add_argument("--no-native", action="store_true", help="Skip Rust hot-path accelerator and use pure-Python hooks")
    ap.add_argument("--bump", choices=("patch", "minor"), default=None, help="Bump version across all manifests so Claude/Codex plugin caches refresh cleanly")
    ap.add_argument("--uninstall", action="store_true", help="Uninstall from selected targets")
    ap.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = ap.parse_args(argv)

    if not any((args.antigravity, args.codex, args.claude, args.all, args.build_dist, args.build_native, args.bump)):
        ap.print_help()
        return 0

    if args.bump:
        bump_version(args.bump)
    if args.build_native:
        ensure_native(force_rebuild=True)
    elif not args.no_native and not args.uninstall and not args.dry_run and any((args.antigravity, args.codex, args.all)):
        ensure_native(force_rebuild=False)
    if args.build_dist:
        build_dist()
    if args.antigravity or args.all:
        if args.uninstall:
            uninstall_antigravity(dry_run=args.dry_run)
        else:
            install_antigravity(dry_run=args.dry_run)
    if args.codex or args.all:
        if args.uninstall:
            uninstall_codex(dry_run=args.dry_run)
        else:
            install_codex(dry_run=args.dry_run)
    if args.claude or args.all:
        print_claude_instructions()
    return 0


if __name__ == "__main__":
    sys.exit(main())
