"""Terser — Fast AST Index Bridge.

Provides lightweight, zero-heavy-import hooks for Terser's AST code index:
  1. Fire-and-forget incremental file re-indexing after Edit/Write/MultiEdit.
  2. Ultra-fast (<5ms) workspace index status lookup directly from ~/.code-index metadata
     (no MCP server spawn required!).
  3. Index-backed large Grep answering on Claude Code / Codex when the AST index package is installed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from runtime.config import normalize_workspace

_CODE_EXTS = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
    ".rb", ".php", ".swift", ".scala", ".lua", ".dart", ".zig", ".gd", ".sql",
})


def code_index_dir(env: dict[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    raw = (env.get("CODE_INDEX_PATH") or "").strip()
    return Path(os.path.expanduser(raw)) if raw else (Path.home() / ".code-index")


def find_index_cmd() -> list[str] | None:
    exe = shutil.which("jcodemunch-mcp")
    if exe:
        return [exe]
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "jcodemunch-mcp"]
    return None


def spawn_reindex_file(file_path: str, *, env: dict[str, str] | None = None) -> bool:
    """Fire-and-forget incremental AST re-indexing on code edits inside indexed repos."""
    if not file_path or "\x00" in file_path:
        return False
    p = Path(file_path)
    if p.suffix.lower() not in _CODE_EXTS:
        return False
    # Fast <5ms check: skip spawning a subprocess if this file is not inside an indexed repo
    if workspace_index_summary(str(p.parent), env=env) is None:
        return False
    exe = shutil.which("jcodemunch-mcp")
    if not exe:
        # Never spawn ephemeral `uvx` on every file edit (too slow for a PostToolUse hook)
        return False
    try:
        kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([exe, "index-file", file_path], **kwargs)
        return True
    except (OSError, ValueError):
        return False


def workspace_index_summary(workspace: str, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Check ~/.code-index metadata for a matching indexed repo in <5ms (zero subprocess)."""
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
                # Skip huge full-index JSON files; read metadata header only if small
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
    """If the AST index package is importable and the repo is indexed, answer huge Greps from the index once."""
    try:
        os.environ.setdefault("JCODEMUNCH_HOOK_GREP_ANSWER", "1")
        from jcodemunch_mcp.cli.hooks.grep_answer import answer_grep  # type: ignore

        return answer_grep(tool_input, cwd, session_id)
    except Exception:
        return None
