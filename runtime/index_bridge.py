"""Terser — Fast Built-In AST Index Bridge.

Connects Terser lifecycle hooks to the bundled `runtime/index_mcp.py` server:
  1. Fire-and-forget incremental file re-indexing after Edit/Write/MultiEdit.
  2. Ultra-fast (<5ms) workspace index status lookup directly from ~/.terser/code-index
     metadata (no MCP server spawn required!).
  3. Index-backed large Grep answering on Claude Code / Codex.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from runtime.config import normalize_workspace, state_home

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_INDEX_MCP_PY = _PLUGIN_ROOT / "runtime" / "index_mcp.py"

_CODE_EXTS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
    ".rb", ".php", ".swift", ".scala", ".lua", ".dart", ".zig", ".gd", ".sql",
})


def code_index_dir(env: dict[str, str] | None = None) -> Path:
    e = env if env is not None else os.environ
    raw = (e.get("TERSER_INDEX_PATH") or "").strip()
    return Path(os.path.expanduser(raw)) if raw else (state_home(e) / "code-index")


def find_index_cmd() -> list[str] | None:
    if _INDEX_MCP_PY.is_file():
        return [sys.executable, "-E", "-s", "-X", "utf8", str(_INDEX_MCP_PY)]
    return None


def spawn_reindex_file(file_path: str, *, env: dict[str, str] | None = None) -> bool:
    """Fire-and-forget incremental AST re-indexing on code edits inside indexed repos."""
    if not file_path or "\x00" in file_path or not _INDEX_MCP_PY.is_file():
        return False
    p = Path(file_path)
    if p.suffix.lower() not in _CODE_EXTS:
        return False
    if workspace_index_summary(str(p.parent), env=env) is None:
        return False
    try:
        from runtime.config import detached_spawn_kwargs, windowless_python

        py_exe = windowless_python(sys.executable)
        base_kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
        }
        if env is not None:
            base_kwargs["env"] = env
        cmd = [py_exe, "-E", "-s", "-X", "utf8", str(_INDEX_MCP_PY), "index-file", file_path]
        try:
            subprocess.Popen(cmd, **base_kwargs, **detached_spawn_kwargs(breakaway=True))
        except OSError:
            subprocess.Popen(cmd, **base_kwargs, **detached_spawn_kwargs(breakaway=False))
        return True
    except (OSError, ValueError):
        return False


def workspace_index_summary(workspace: str, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Check ~/.terser/code-index metadata for a matching indexed repo in <5ms (zero subprocess)."""
    target = normalize_workspace(workspace)
    if not target:
        return None
    idx_dir = code_index_dir(env)
    if not idx_dir.is_dir():
        return None
    try:
        for meta_file in idx_dir.glob("*.json"):
            if meta_file.name.startswith("_") or meta_file.name in ("config.json", "session_stats.json"):
                continue
            try:
                raw = meta_file.read_bytes()
                if len(raw) > 512 * 1024:
                    head = raw[:4096].decode("utf-8", errors="ignore")
                    if target.split("/")[-1] not in head.lower():
                        continue
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                else:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            sr = normalize_workspace(str(data.get("source_root") or ""))
            if not sr:
                continue
            if target == sr or target.startswith(sr + "/") or sr.startswith(target + "/"):
                repo_id = str(data.get("repo") or meta_file.stem)
                sym_count = len(data.get("symbols") or []) if isinstance(data.get("symbols"), list) else int(data.get("symbol_count") or 0)
                file_count = len(data.get("source_files") or []) if isinstance(data.get("source_files"), list) else int(data.get("file_count") or 0)
                return {
                    "repo": repo_id,
                    "source_root": sr,
                    "symbols": sym_count,
                    "files": file_count,
                }
    except OSError:
        return None
    return None


def try_answer_large_grep(tool_input: dict, cwd: str, session_id: str) -> str | None:
    """If the workspace is indexed in ~/.terser/code-index, answer matching Grep queries directly."""
    pattern = str(tool_input.get("pattern") or "").strip()
    if not pattern or len(pattern) < 3 or any(c in pattern for c in "*?[]()^$\\|"):
        return None
    idx = workspace_index_summary(cwd)
    if not idx:
        return None
    try:
        from runtime import index_mcp

        res = index_mcp.dispatch_action("search_text", {"repo": idx["repo"], "query": pattern})
        matches = res.get("matches") or []
        if len(matches) < 25:
            return None
        lines = [f"[Terser Index] Fast match summary for `{pattern}` ({len(matches)} hits in `{idx['repo']}`):"]
        for m in matches[:25]:
            lines.append(f"  {m['file']}:{m['line']}: {m['text']}")
        if len(matches) > 25:
            lines.append(f"  ... ({len(matches) - 25} more; use `terser-index` `search_symbols` or narrow path)")
        return "\n".join(lines)
    except Exception:
        return None
