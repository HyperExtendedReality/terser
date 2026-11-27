#!/usr/bin/env python3
"""Terser — Cross-Platform Installer & Bundle Builder.

Installs Terser cleanly and reversibly into:
  - Antigravity 2.0 / IDE / agy  (--antigravity) -> ~/.gemini/config/plugins/terser/
  - Claude Code                  (--claude)      -> builds self-contained bundle & prints/runs install
  - Codex                        (--codex)       -> prepares a local marketplace and migrates legacy global entries
  - opencode                     (--opencode)    -> ~/.config/opencode/plugins/terser.js
  - Cursor                       (--cursor)      -> merges ~/.cursor/hooks.json and ~/.cursor/mcp.json
  - Host health check            (--doctor)      -> with --opencode and/or --cursor
  - All three                    (--all)
  - Standalone dist bundles      (--build-dist)  -> dist/antigravity, dist/claude-codex, dist/codex

Never touches unrelated user hooks or settings. Idempotent and reversible (--uninstall).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
IGNORE = shutil.ignore_patterns(
    ".git", "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".DS_Store", "dist", "tests",
)


def _resolve_python() -> str:
    exe = os.path.realpath(sys.executable)
    if sys.platform == "win32" and exe:
        folder, name = os.path.split(exe)
        candidates = ["pythonw.exe"]
        if name.lower().startswith("python") and not name.lower().startswith("pythonw"):
            candidates.insert(0, "pythonw" + name[len("python"):])
        for cand in candidates:
            path = os.path.join(folder, cand)
            if os.path.isfile(path):
                return path
    return exe


def _antigravity_home() -> Path:
    raw = os.environ.get("TOKEN_OPTIMIZER_ANTIGRAVITY_HOME") or ""
    return Path(os.path.expanduser(raw)) if raw else (Path.home() / ".gemini")


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME") or ""
    return Path(os.path.expanduser(raw)) if raw else (Path.home() / ".codex")


_HOT_EVENTS = frozenset({"PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart", "SubagentStop"})
_RELEASE_BASE = "https://github.com/HyperExtendedReality/terser/releases/latest/download"


def install_statusline(*, dry_run: bool = False, uninstall: bool = False) -> None:
    """Point Claude Code's statusLine at Terser, never replacing someone else's."""
    home = Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
    path = home / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except ValueError:
        print(f"[statusline] {path} is not valid JSON; left unchanged.")
        return
    script = PLUGIN_ROOT / "runtime" / "statusline.py"
    current = data.get("statusLine")
    ours = isinstance(current, dict) and "statusline.py" in str(current.get("command", ""))
    if uninstall:
        if ours and not dry_run:
            data.pop("statusLine", None)
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"[statusline] {'removed' if ours else 'not installed'} ({path})")
        return
    if current and not ours:
        print(f"[statusline] {path} already has a statusLine; left unchanged.")
        return
    cmd = f'"{sys.executable}" -E -s "{script}"'
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        data["statusLine"] = {"type": "command", "command": cmd}
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"[statusline] {'would set' if dry_run else 'set'} statusLine in {path}")


def _native_bin_name() -> str:
    return "terser-fast.exe" if os.name == "nt" else "terser-fast"


def _native_bin_path(plugin_dir: Path) -> Path:
    return plugin_dir / "bin" / _native_bin_name()


# (os family, normalized arch) -> release asset. Must match .github/workflows/release.yml.
_RELEASE_ASSETS = {
    ("linux", "x86_64"): "terser-fast-linux-x86_64",
    ("linux", "aarch64"): "terser-fast-linux-aarch64",
    ("darwin", "x86_64"): "terser-fast-macos-x86_64",
    ("darwin", "aarch64"): "terser-fast-macos-aarch64",
    ("win32", "x86_64"): "terser-fast-windows-x86_64.exe",
    ("win32", "aarch64"): "terser-fast-windows-aarch64.exe",
}
_ARCH_ALIASES = {"amd64": "x86_64", "x86_64": "x86_64", "x64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}


def _release_asset_name(plat: str | None = None, machine: str | None = None) -> str | None:
    import platform

    plat = plat if plat is not None else sys.platform
    arch = _ARCH_ALIASES.get((machine if machine is not None else platform.machine()).lower())
    family = "linux" if plat.startswith("linux") else plat
    if not arch:
        return None
    return _RELEASE_ASSETS.get((family, arch))


def _expected_sha256(asset: str) -> str | None:
    """Fetch the release's SHA256SUMS and return the digest listed for ``asset``."""
    import urllib.request

    req = urllib.request.Request(f"{_RELEASE_BASE}/SHA256SUMS", headers={"User-Agent": "terser-installer/1.2"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        if getattr(resp, "status", 200) != 200:
            return None
        text = resp.read(65_536).decode("utf-8", "replace")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == asset and len(parts[0]) == 64:
            return parts[0].lower()
    return None


def _checksum_ok(asset: str, data: bytes) -> bool:
    """Fail closed: a binary without a matching published digest is never installed."""
    import hashlib

    try:
        expected = _expected_sha256(asset)
    except Exception:
        return False
    return bool(expected) and hashlib.sha256(data).hexdigest() == expected


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
        if not _checksum_ok(asset, data):
            print("[Native] Prebuilt binary failed SHA-256 verification; using pure Python.")
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
                "matcher": "run_command|read_file|view_file|call_mcp_tool|mcp_.*|invoke_subagent|swarm|define_subagent",
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


_CODEX_BEGIN = "# BEGIN terser"
_CODEX_END = "# END terser"
_CODEX_EXACT_HOOK = re.compile(
    r'^"?(?P<exe>[^"]+?)"? +(?:-E -s -X utf8 |--python "[^"]+" --script )'
    r'"(?P<script>[^"]+)" --host codex --event (?P<event>[A-Za-z]+)$'
)
_CODEX_MARKETPLACE = PLUGIN_ROOT / "dist" / "codex"


def _parse_codex_toml(text: str) -> dict[str, Any]:
    try:
        import tomllib
    except ImportError as exc:
        raise ValueError("Codex config migration requires Python 3.11 or later") from exc
    return tomllib.loads(text)


def _legacy_codex_hook(hook: Any, event: str) -> bool:
    if not isinstance(hook, dict) or hook.get("type") != "command":
        return False
    match = _CODEX_EXACT_HOOK.fullmatch(str(hook.get("command") or ""))
    if not match or match["event"] != event:
        return False
    script = Path(match["script"])
    if not script.is_absolute() or script.name != "unified_hook.py" or script.parent.name != "runtime":
        return False
    root = script.parent.parent
    if "--script " in match[0] and Path(match["exe"]) != _native_bin_path(root):
        return False
    try:
        manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        return manifest.get("name") == "terser"
    except (OSError, ValueError, AttributeError):
        return False


def _strip_legacy_codex_hooks(text: str) -> str:
    doc = json.loads(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("hooks", {}), dict):
        raise ValueError("Invalid Codex hooks.json")
    changed = False
    for event, entries in doc.get("hooks", {}).items():
        if not isinstance(entries, list):
            raise ValueError(f"Invalid Codex hook list for {event}")
        kept = []
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                hooks = entry["hooks"]
                remaining = [hook for hook in hooks if not _legacy_codex_hook(hook, event)]
                if remaining != hooks:
                    changed = True
                    if remaining:
                        kept.append(dict(entry, hooks=remaining))
                    continue
            kept.append(entry)
        doc["hooks"][event] = kept
    return json.dumps(doc, indent=2) + "\n" if changed else text


def _strip_codex_toml_block(text: str) -> str:
    # The previous installer used these exact sentinel comments around one
    # Terser server table. Leave all other tables and comments intact.
    lines = text.splitlines(keepends=True)
    kept: list[str] = []
    managed: list[str] | None = None
    for line in lines:
        marker = line.strip()
        if marker == _CODEX_BEGIN:
            if managed is not None:
                raise ValueError("Nested legacy Terser config markers")
            managed = []
        elif marker == _CODEX_END:
            if managed is None:
                raise ValueError("Unmatched legacy Terser config marker")
            block = _parse_codex_toml("".join(managed))
            servers = block.get("mcp_servers", {})
            definition = servers.get("terser-index", {})
            args = definition.get("args", [])
            if (set(block) != {"mcp_servers"} or set(servers) != {"terser-index"}
                    or not isinstance(args, list) or not args
                    or Path(str(args[-1])).name != "index_mcp.py"):
                raise ValueError("Legacy Terser config markers surround an unrecognized definition")
            managed = None
        elif managed is None:
            kept.append(line)
        else:
            managed.append(line)
    if managed is not None:
        raise ValueError("Unclosed legacy Terser config block")
    cleaned = "".join(kept)
    _parse_codex_toml(cleaned)
    return cleaned


def _write_codex_config(path: Path, original: str, replacement: str) -> None:
    if original == replacement:
        return
    if path.read_bytes() != original.encode("utf-8"):
        raise ValueError(f"Codex configuration changed during migration: {path}")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        staged = Path(handle.name)
        handle.write(replacement.encode("utf-8"))
    try:
        staged.chmod(stat.S_IMODE(path.stat().st_mode))
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def _migrate_codex_config(*, dry_run: bool = False) -> None:
    home = _codex_home()
    updates = []
    for name, clean in (("hooks.json", _strip_legacy_codex_hooks), ("config.toml", _strip_codex_toml_block)):
        path = home / name
        if path.is_file():
            original = path.read_bytes().decode("utf-8")
            cleaned = clean(original)
            if original != cleaned:
                updates.append((path, original, cleaned))
    # Validate both files before modifying either, and preserve formatting for untouched files.
    if not dry_run:
        applied = []
        try:
            for path, original, cleaned in updates:
                _write_codex_config(path, original, cleaned)
                applied.append((path, original, cleaned))
        except (OSError, ValueError) as exc:
            for path, original, cleaned in reversed(applied):
                try:
                    _write_codex_config(path, cleaned, original)
                except (OSError, ValueError) as restore_error:
                    raise ValueError(f"{exc}; could not restore {path}: {restore_error}") from exc
            raise
    for path, _, _ in updates:
        print(f"  {'Would remove' if dry_run else 'Removed'} legacy Terser entries in {path}")


def _supports_codex_hooks(binary: Path) -> bool:
    if not binary.is_file():
        return False
    try:
        probe = subprocess.run([str(binary), "--codex-hook-protocol"], input=b"", capture_output=True, timeout=3)
        return probe.returncode == 0 and probe.stdout.strip() == b"1"
    except (OSError, subprocess.TimeoutExpired):
        return False


def build_codex_marketplace(target: Path, *, use_native: bool = True) -> Path:
    bundle = target / "terser"
    for directory in (".codex-plugin", "assets", "hooks", "rules", "runtime", "scripts", "skills", "vendor"):
        _sync_tree(PLUGIN_ROOT / directory, bundle / directory, ignore=IGNORE)
    shutil.copy2(PLUGIN_ROOT / "LICENSE", bundle)
    config = json.loads((PLUGIN_ROOT / "codex-mcp.json").read_text(encoding="utf-8"))
    python = os.path.realpath(sys.executable)
    config["mcpServers"]["terser-index"]["command"] = python
    (bundle / "codex-mcp.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    (bundle / "runtime" / "codex-python.txt").write_text(python + "\n", encoding="utf-8")
    for name in ("terse", "terse-hook"):
        launcher = bundle / "scripts" / name
        data = launcher.read_bytes()
        if b"\r\n" in data:
            launcher.write_bytes(data.replace(b"\r\n", b"\n"))
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    native = _native_bin_path(PLUGIN_ROOT)
    staged = bundle / "bin" / _native_bin_name().replace("terser-fast", "terser-codex")
    if use_native and _supports_codex_hooks(native):
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(native, staged)
    else:
        staged.unlink(missing_ok=True)

    market = target / ".agents" / "plugins" / "marketplace.json"
    market.parent.mkdir(parents=True, exist_ok=True)
    market.write_text(json.dumps({
        "name": "terser",
        "plugins": [{"name": "terser", "source": {"source": "local", "path": "./terser"}}],
    }, indent=2) + "\n", encoding="utf-8")
    return target


def install_codex(*, dry_run: bool = False, use_native: bool = True) -> Path:
    target = _CODEX_MARKETPLACE
    if dry_run:
        print(f"[Codex] Would build the marketplace in {target}")
    else:
        build_codex_marketplace(target, use_native=use_native)
        print(f"[Codex] Prepared the marketplace in {target}")
    _migrate_codex_config(dry_run=dry_run)
    print(f'  codex plugin marketplace add "{target}"')
    print("  codex plugin add terser@terser")
    return target


def uninstall_codex(*, dry_run: bool = False) -> None:
    _migrate_codex_config(dry_run=dry_run)
    print("[Codex] Remove the marketplace plugin with `codex plugin remove terser@terser`.")


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
    codex_bundle = build_codex_marketplace(dist / "codex")

    print(f"\nBuilt standalone bundles:")
    print(f"  Antigravity 2.0 : {ag_bundle}")
    print(f"  Claude & Codex  : {dist / 'claude-codex'}")
    print(f"  Codex marketplace: {codex_bundle}")
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
    ap.add_argument("--codex", action="store_true", help="Prepare the Codex marketplace and remove legacy global Terser entries")
    ap.add_argument("--claude", action="store_true", help="Print Claude Code plugin install/update commands")
    ap.add_argument("--opencode", action="store_true", help="Install/update the opencode plugin (~/.config/opencode/plugins/terser.js)")
    ap.add_argument("--cursor", action="store_true", help="Merge terser hooks + terser-index MCP into ~/.cursor/hooks.json and mcp.json")
    ap.add_argument("--doctor", action="store_true", help="Check the selected --opencode/--cursor install end to end")
    ap.add_argument("--all", action="store_true", help="Install/configure for Antigravity 2.0, Codex, and Claude Code")
    ap.add_argument("--build-dist", action="store_true", help="Build standalone dist/ bundles for all 3 platforms")
    ap.add_argument("--build-native", action="store_true", help="Force-rebuild the Rust hot-path accelerator (bin/terser-fast)")
    ap.add_argument("--no-native", action="store_true", help="Skip Rust hot-path accelerator and use pure-Python hooks")
    ap.add_argument("--bump", choices=("patch", "minor"), default=None, help="Bump version across all manifests so Claude/Codex plugin caches refresh cleanly")
    ap.add_argument("--uninstall", action="store_true", help="Uninstall from selected targets")
    ap.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    ap.add_argument("--statusline", action="store_true", help="Set Terser as the Claude Code status line (only if none is set)")
    args = ap.parse_args(argv)

    if not any((args.antigravity, args.codex, args.claude, args.all, args.build_dist, args.build_native, args.bump,
                args.opencode, args.cursor, args.statusline)):
        ap.print_help()
        return 0

    if args.opencode or args.cursor:
        sys.path.insert(0, str(PLUGIN_ROOT))
        from runtime import host_install

        python_exe = os.path.realpath(sys.executable)
        if args.doctor:
            ok = True
            for host, on in (("opencode", args.opencode), ("cursor", args.cursor)):
                if on:
                    ok = host_install.doctor(host, python_exe) and ok
            return 0 if ok else 1
        try:
            if args.opencode:
                if args.uninstall:
                    host_install.uninstall_opencode(dry_run=args.dry_run)
                else:
                    host_install.install_opencode(python_exe, dry_run=args.dry_run)
            if args.cursor:
                if args.uninstall:
                    host_install.uninstall_cursor(dry_run=args.dry_run)
                else:
                    host_install.install_cursor(python_exe, dry_run=args.dry_run)
        except (OSError, ValueError) as exc:
            print(f"[Hosts] {exc}", file=sys.stderr)
            return 1

    if args.bump:
        bump_version(args.bump)
    if args.build_native:
        ensure_native(force_rebuild=True)
    elif not args.no_native and not args.uninstall and not args.dry_run and any((args.antigravity, args.codex, args.all)):
        native = ensure_native(force_rebuild=False)
        if (args.codex or args.all) and native and not _supports_codex_hooks(native):
            # Legacy binaries remain usable by other hosts. Prefer a fresh
            # build for Codex when a local toolchain is available.
            build_native()
    if args.build_dist:
        build_dist()
    if args.antigravity or args.all:
        if args.uninstall:
            uninstall_antigravity(dry_run=args.dry_run)
        else:
            install_antigravity(dry_run=args.dry_run)
    if args.codex or args.all:
        try:
            if args.uninstall:
                uninstall_codex(dry_run=args.dry_run)
            else:
                install_codex(dry_run=args.dry_run, use_native=not args.no_native)
        except (OSError, ValueError) as exc:
            print(f"[Codex] {exc}", file=sys.stderr)
            return 1
    if args.claude or args.all:
        print_claude_instructions()
    if args.statusline:
        install_statusline(dry_run=args.dry_run, uninstall=args.uninstall)
    return 0


if __name__ == "__main__":
    sys.exit(main())
