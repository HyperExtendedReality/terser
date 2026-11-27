"""Read-only diagnostics for the Codex marketplace installation."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any


def _app_status(codex: str, cwd: str) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    messages: queue.Queue[str | None] = queue.Queue()
    with subprocess.Popen(
        [codex, "app-server"], cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
    ) as proc:
        def read_stdout() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                messages.put(line)
            messages.put(None)

        threading.Thread(target=read_stdout, daemon=True).start()
        assert proc.stdin is not None
        try:
            def request(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
                proc.stdin.flush()
                while True:
                    line = messages.get(timeout=5)
                    if line is None:
                        raise ValueError("Codex app-server closed before replying")
                    response = json.loads(line)
                    if response.get("id") == request_id:
                        if "error" in response:
                            raise ValueError(f"Codex could not read {method}")
                        return response["result"]

            request(1, "initialize", {"clientInfo": {"name": "terser-doctor", "version": "1"}})
            proc.stdin.write('{"jsonrpc":"2.0","method":"initialized"}\n')
            proc.stdin.flush()
            hooks = request(2, "hooks/list", {"cwds": [cwd]})
            config = request(3, "config/read", {"cwd": cwd}).get("config") or {}
            reports = hooks.get("data", [])
            installed = any(hook.get("pluginId") == "terser@terser" for report in reports for hook in report["hooks"])
            servers = []
            if installed and "terser-index" not in (config.get("mcp_servers") or {}):
                try:
                    status = request(4, "mcpServerStatus/list", {"serverName": "terser-index"})
                    servers = status.get("data", [])
                except (ValueError, queue.Empty):
                    pass
            return reports, config, servers
        finally:
            if proc.poll() is None:
                proc.kill()


def _probe_mcp(root: Path, cwd: str) -> int:
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    config = json.loads((root / manifest["mcpServers"]).read_text(encoding="utf-8"))
    server = config["mcpServers"]["terser-index"]
    def expand(arg: str) -> str:
        return arg.replace("${PLUGIN_ROOT}", str(root)).replace("${CLAUDE_PLUGIN_ROOT}", str(root))
    command = [expand(server["command"]), *(expand(arg) for arg in server["args"])]
    rpc = (
        '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}\n'
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        '{"jsonrpc":"2.0","id":3,"method":"ping"}\n'
    )
    run = subprocess.run(command, input=rpc, cwd=cwd, env=dict(os.environ, **server.get("env", {})),
                         text=True, encoding="utf-8", capture_output=True, timeout=5, check=True)
    results = {item.get("id"): item.get("result") for item in map(json.loads, run.stdout.splitlines())}
    if results[1]["serverInfo"]["name"] != "terser-index" or results[3] != {}:
        raise ValueError("Unexpected MCP handshake")
    names = {tool["name"] for tool in results[2]["tools"]}
    if names != {"menu", "route", "order", "index_folder", "search_symbols", "get_symbol_source"}:
        raise ValueError("Unexpected MCP tool surface")
    return len(names)


def run(root: Path, cwd: str) -> bool:
    codex = shutil.which("codex")
    legacy_hooks = 0
    config: dict[str, Any] = {}
    plugin_hooks: list[dict[str, Any]] = []
    ok = True
    try:
        if not codex:
            raise ValueError("Codex executable is unavailable")
        reports, config, servers = _app_status(codex, cwd)
        for report in reports:
            for hook in report["hooks"]:
                if hook.get("pluginId") == "terser@terser":
                    plugin_hooks.append(hook)
                elif "runtime/unified_hook.py" in str(hook.get("command") or "").replace("\\", "/") and "--host codex" in hook["command"]:
                    from scripts.install import _legacy_codex_hook
                    legacy_hooks += _legacy_codex_hook(dict(hook, type="command"), hook["eventName"])
        if plugin_hooks:
            trust = sorted({hook["trustStatus"] for hook in plugin_hooks})
            active = sum(hook["enabled"] and hook["trustStatus"] in ("trusted", "managed") for hook in plugin_hooks)
            level = "PASS" if active == len(plugin_hooks) else "WARN"
            print(f"  [{level}] Codex hook trust: {active}/{len(plugin_hooks)} active ({', '.join(trust)})")
            root = Path(plugin_hooks[0]["sourcePath"]).parent.parent
        else:
            print("  [WARN] Codex hook trust: no Terser marketplace hooks discovered")
        global_mcp = "terser-index" in (config.get("mcp_servers") or {})
        if legacy_hooks or global_mcp:
            print(f"  [WARN] Codex duplicate registration risk: {legacy_hooks} legacy hooks, global MCP={global_mcp}")
        counts: dict[str, int] = {}
        for hook in plugin_hooks:
            key = hook["eventName"] + ":" + str(hook.get("matcher"))
            counts[key] = counts.get(key, 0) + 1
        if any(count > 1 for count in counts.values()):
            print("  [WARN] Codex returned duplicate Terser plugin hook registrations")
        installed_server = next((server for server in servers if server.get("pluginId") == "terser@terser"), None)
        if installed_server is not None:
            tools = len(installed_server.get("tools", {}))
            if tools == 6:
                print("  [PASS] Codex MCP registration: 6 tools discovered by the host")
            else:
                print(f"  [FAIL] Codex MCP registration: {installed_server.get('toolsError') or f'{tools} tools discovered'}")
                ok = False
        elif plugin_hooks and not global_mcp:
            print("  [WARN] Codex MCP registration: the host did not report the plugin server")
    except (OSError, ValueError, KeyError, queue.Empty) as exc:
        print(f"  [WARN] Codex hook trust: inspection unavailable ({str(exc) or type(exc).__name__})")
    try:
        print(f"  [PASS] Codex MCP handshake: {_probe_mcp(root, cwd)} tools")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        print(f"  [FAIL] Codex MCP handshake: check the interpreter configured in {root / 'codex-mcp.json'}")
        return False
    return ok
