"""Install, uninstall and doctor checks for the opencode and Cursor hosts.

Used by ``scripts/install.py --opencode`` / ``--cursor`` and ``--doctor``.
Every write is a merge. Only entries terser owns are touched: the
``terser.js`` plugin file, hooks whose command names
``unified_hook.py --host cursor``, and the ``terser-index`` MCP entry. Unrelated
user settings are kept.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

RUNTIME_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = RUNTIME_DIR.parent
_PLUGIN_TEMPLATE = PLUGIN_ROOT / "hooks" / "opencode-plugin.js"

# Cursor event -> timeout seconds. Event names from https://cursor.com/docs/agent/hooks.
CURSOR_EVENTS = {
    "sessionStart": 10,
    "preToolUse": 10,
    "postToolUse": 10,
    "postToolUseFailure": 10,
    "subagentStart": 10,
    "subagentStop": 10,
    "preCompact": 10,
    "stop": 15,
    "sessionEnd": 10,
}
_CURSOR_TOOL_MATCHER = "Shell|Read|Write|Grep|MCP:.*"
_CURSOR_MARK = "--host cursor"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def opencode_config_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    raw = env.get("OPENCODE_CONFIG_DIR") or ""
    if raw:
        return Path(os.path.expanduser(raw))
    xdg = env.get("XDG_CONFIG_HOME") or ""
    base = Path(os.path.expanduser(xdg)) if xdg else Path.home() / ".config"
    return base / "opencode"


def opencode_plugin_path(env: dict[str, str] | None = None) -> Path:
    return opencode_config_dir(env) / "plugins" / "terser.js"


def cursor_home(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    raw = env.get("CURSOR_HOME") or ""
    return Path(os.path.expanduser(raw)) if raw else Path.home() / ".cursor"


def _hook_script(root: Path) -> str:
    return str(root / "runtime" / "unified_hook.py")


def _index_script(root: Path) -> str:
    return str(root / "runtime" / "index_mcp.py")


def _q(path: str) -> str:
    return f'"{path}"'


# ---------------------------------------------------------------------------
# opencode
# ---------------------------------------------------------------------------

def render_opencode_plugin(python_exe: str, root: Path = PLUGIN_ROOT) -> str:
    text = _PLUGIN_TEMPLATE.read_text(encoding="utf-8")
    return (
        text.replace("__TERSER_PYTHON__", json.dumps(python_exe))
        .replace("__TERSER_HOOK__", json.dumps(_hook_script(root)))
    )


def opencode_mcp_snippet(python_exe: str, root: Path = PLUGIN_ROOT) -> dict[str, Any]:
    """The terser-index MCP entry in current-docs (``mcp.servers``) form."""
    return {
        "mcp": {
            "servers": {
                "terser-index": {
                    "type": "local",
                    "command": [python_exe, "-E", "-s", "-X", "utf8", _index_script(root)],
                    "environment": {"TERSER_CLIENT_ID": "opencode", "TERSER_TOOL_SURFACE": "counter"},
                }
            }
        }
    }


def install_opencode(python_exe: str, *, dry_run: bool = False, env: dict[str, str] | None = None) -> Path:
    dest = opencode_plugin_path(env)
    print(f"[opencode] Plugin file: {dest}")
    if dry_run:
        print("  (dry-run) Would write terser.js plugin routing hooks to unified_hook.py --host opencode")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_opencode_plugin(python_exe), encoding="utf-8")
    print("  Installed. Restart opencode to load it.")
    # The MCP config key moved between opencode versions (`mcp.<name>` vs
    # `mcp.servers.<name>`), and a wrong key can make opencode reject the whole
    # config. So the entry is printed for the user to add, not written.
    print("  Optional AST index: add this to ~/.config/opencode/opencode.json:")
    print("  " + json.dumps(opencode_mcp_snippet(python_exe)))
    return dest


def uninstall_opencode(*, dry_run: bool = False, env: dict[str, str] | None = None) -> None:
    dest = opencode_plugin_path(env)
    if not dest.exists():
        print(f"[opencode] Not installed at {dest}")
        return
    if dry_run:
        print(f"[opencode] (dry-run) Would remove {dest}")
        return
    dest.unlink()
    print(f"[opencode] Removed {dest}")


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------

def cursor_hook_command(python_exe: str, event: str, root: Path = PLUGIN_ROOT, *, windows: bool | None = None) -> str:
    # Cursor's desktop app runs hook commands through PowerShell on Windows, where
    # a quoted executable path needs the call operator `&`.
    windows = (os.name == "nt") if windows is None else windows
    call = "& " if windows else ""
    return f"{call}{_q(python_exe)} -E -s -X utf8 {_q(_hook_script(root))} {_CURSOR_MARK} --event {event}"


def _is_terser_cursor_hook(entry: Any) -> bool:
    return isinstance(entry, dict) and _CURSOR_MARK in str(entry.get("command") or "") and "unified_hook.py" in str(
        entry.get("command") or ""
    )


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)  # Malformed user JSON raises: never overwrite what we cannot parse.
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def merge_cursor_hooks(doc: dict[str, Any], python_exe: str, root: Path = PLUGIN_ROOT, *, remove: bool = False) -> dict[str, Any]:
    out = dict(doc)
    out.setdefault("version", 1)
    hooks = dict(out.get("hooks") or {})
    for event in set(hooks) | (set() if remove else set(CURSOR_EVENTS)):
        entries = [e for e in (hooks.get(event) or []) if not _is_terser_cursor_hook(e)]
        if not remove and event in CURSOR_EVENTS:
            entry: dict[str, Any] = {"command": cursor_hook_command(python_exe, event, root), "timeout": CURSOR_EVENTS[event]}
            if event in ("preToolUse", "postToolUse", "postToolUseFailure"):
                entry["matcher"] = _CURSOR_TOOL_MATCHER
            entries.append(entry)
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    out["hooks"] = hooks
    return out


def merge_cursor_mcp(doc: dict[str, Any], python_exe: str, root: Path = PLUGIN_ROOT, *, remove: bool = False) -> dict[str, Any]:
    out = dict(doc)
    servers = dict(out.get("mcpServers") or {})
    if remove:
        servers.pop("terser-index", None)
    else:
        servers["terser-index"] = {
            "type": "stdio",
            "command": python_exe,
            "args": ["-E", "-s", "-X", "utf8", _index_script(root)],
            "env": {"TERSER_CLIENT_ID": "cursor", "TERSER_TOOL_SURFACE": "counter"},
        }
    out["mcpServers"] = servers
    return out


def install_cursor(python_exe: str, *, dry_run: bool = False, env: dict[str, str] | None = None) -> Path:
    home = cursor_home(env)
    hooks_path, mcp_path = home / "hooks.json", home / "mcp.json"
    print(f"[Cursor] Hooks: {hooks_path}  MCP: {mcp_path}")
    if dry_run:
        print(f"  (dry-run) Would merge {len(CURSOR_EVENTS)} terser hooks and the terser-index MCP server")
        return hooks_path
    _write_json(hooks_path, merge_cursor_hooks(_load_json(hooks_path), python_exe))
    _write_json(mcp_path, merge_cursor_mcp(_load_json(mcp_path), python_exe))
    print("  Installed. Cursor reloads hooks.json on save. Restart it to pick up the MCP server.")
    return hooks_path


def uninstall_cursor(*, dry_run: bool = False, env: dict[str, str] | None = None) -> None:
    home = cursor_home(env)
    hooks_path, mcp_path = home / "hooks.json", home / "mcp.json"
    if dry_run:
        print(f"[Cursor] (dry-run) Would remove terser entries from {hooks_path} and {mcp_path}")
        return
    if hooks_path.is_file():
        _write_json(hooks_path, merge_cursor_hooks(_load_json(hooks_path), "", remove=True))
    if mcp_path.is_file():
        _write_json(mcp_path, merge_cursor_mcp(_load_json(mcp_path), "", remove=True))
    print(f"[Cursor] Removed terser entries from {home}")


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

def _probe_hook(python_exe: str, host: str, event: str, payload: dict[str, Any]) -> tuple[bool, str]:
    """Run the hook end to end, the way the host would, with a harmless payload."""
    try:
        res = subprocess.run(
            [python_exe, "-E", "-s", "-X", "utf8", _hook_script(PLUGIN_ROOT), "--host", host, "--event", event],
            input=json.dumps(payload), capture_output=True, text=True, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
        return False, f"hook did not run: {exc}"
    text = (res.stdout or "").strip()
    try:
        reply = json.loads(text) if text else {}
    except ValueError:
        return False, f"hook printed non-JSON: {text[:120]!r}"
    return True, json.dumps(reply)


def doctor(host: str, python_exe: str, env: dict[str, str] | None = None) -> bool:
    """Print a check list for one host. Returns True when every check passes."""
    checks: list[tuple[bool, str]] = []
    if host == "opencode":
        path = opencode_plugin_path(env)
        installed = path.is_file()
        checks.append((installed, f"plugin file {path}"))
        if installed:
            text = path.read_text(encoding="utf-8")
            checks.append((json.dumps(_hook_script(PLUGIN_ROOT)) in text,
                           "plugin points at this checkout's unified_hook.py"))
        ok, detail = _probe_hook(python_exe, "opencode", "tool.execute.before",
                                 {"tool": "bash", "sessionID": "terser-doctor", "args": {"command": "echo ok"}})
        checks.append((ok, f"hook round-trip: {detail}"))
    elif host == "cursor":
        hooks_path = cursor_home(env) / "hooks.json"
        try:
            doc = _load_json(hooks_path)
            parse_ok = True
        except Exception as exc:  # noqa: BLE001
            doc, parse_ok = {}, False
            checks.append((False, f"{hooks_path} unreadable: {exc}"))
        if parse_ok:
            hooks = doc.get("hooks") or {}
            missing = [e for e in CURSOR_EVENTS if not any(_is_terser_cursor_hook(h) for h in hooks.get(e) or [])]
            checks.append((not missing, f"hooks.json events wired ({len(CURSOR_EVENTS) - len(missing)}/{len(CURSOR_EVENTS)})"
                           + (f", missing {', '.join(missing)}" if missing else "")))
            checks.append((doc.get("version") == 1, "hooks.json version 1"))
        ok, detail = _probe_hook(python_exe, "cursor", "preToolUse",
                                 {"hook_event_name": "preToolUse", "conversation_id": "terser-doctor",
                                  "tool_name": "Shell", "tool_input": {"command": "echo ok"}})
        checks.append((ok and '"permission"' in detail, f"hook round-trip: {detail}"))
    else:
        print(f"[doctor] Unknown host {host!r}")
        return False
    print(f"[{host} doctor]")
    for ok, label in checks:
        print(f"  {'OK  ' if ok else 'FAIL'} {label}")
    return all(ok for ok, _ in checks)
