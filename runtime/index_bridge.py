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


_AUTO_INDEX_COOLDOWN_S = 6 * 3600


def _spawn_index_cmd(args: list[str], env: dict[str, str] | None) -> bool:
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
    cmd = [py_exe, "-E", "-s", "-X", "utf8", str(_INDEX_MCP_PY), *args]
    try:
        subprocess.Popen(cmd, **base_kwargs, **detached_spawn_kwargs(breakaway=True))
    except OSError:
        subprocess.Popen(cmd, **base_kwargs, **detached_spawn_kwargs(breakaway=False))
    return True


def spawn_auto_index(
    workspace: str,
    *,
    max_files: int = 5000,
    env: dict[str, str] | None = None,
) -> bool:
    """Fire-and-forget first-time AST index of an unindexed git workspace.

    Zero hook latency: the size cap is enforced by the detached child. A per-workspace
    cooldown stamp prevents respawning on every session for oversized or failing repos.
    """
    if not workspace or "\x00" in workspace or not _INDEX_MCP_PY.is_file():
        return False
    try:
        root = Path(workspace).resolve()
    except (OSError, RuntimeError):
        return False
    if not root.is_dir():
        return False
    # Index the enclosing git work tree, so sessions started in a subfolder still qualify.
    for cand in [root, *list(root.parents)[:8]]:
        if (cand / ".git").exists():
            root = cand
            break
    else:
        return False
    home = Path.home().resolve()
    if root == home or root.parent == root:
        return False
    if workspace_index_summary(str(root), env=env) is not None:
        return False
    from runtime.config import workspace_hash

    stamp = state_home(env) / "auto-index" / f"{workspace_hash(str(root))}.stamp"
    try:
        import time

        if stamp.is_file() and time.time() - stamp.stat().st_mtime < _AUTO_INDEX_COOLDOWN_S:
            return False
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(root), encoding="utf-8")
        return _spawn_index_cmd(["auto-index", str(root), str(max(1, int(max_files)))], env)
    except (OSError, ValueError):
        return False


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
        return _spawn_index_cmd(["index-file", file_path], env)
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
        # Prefer tiny ~150B *.meta.json files first (<1ms) before falling back to legacy *.json
        meta_files = list(idx_dir.glob("*.meta.json"))
        legacy_files = [p for p in idx_dir.glob("*.json") if not p.name.endswith(".meta.json")]
        for meta_file in [*meta_files, *legacy_files]:
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
                default_slug = meta_file.name[:-10] if meta_file.name.endswith(".meta.json") else meta_file.stem
                repo_id = str(data.get("repo") or default_slug)
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


_GREP_SCOPE_KEYS = (
    "glob", "type", "-i", "-A", "-B", "-C", "context", "multiline", "head_limit", "offset",
)
_GREP_REGEX_CHARS = set(r"*?[](){}^$\|.+")


def _grep_answer_once(session_id: str, pattern: str) -> bool:
    """True the first time a session sees ``pattern``; an identical retry always runs the real Grep."""
    if not session_id:
        return False
    try:
        from runtime.config import state_home

        d = state_home() / "grep-answer"
        d.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:80]
        f = d / f"{safe}.json"
        seen = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []
        if not isinstance(seen, list):
            seen = []
        if pattern in seen:
            return False
        seen = (seen + [pattern])[-200:]
        f.write_text(json.dumps(seen), encoding="utf-8")
        return True
    except (OSError, ValueError):
        return False


def try_answer_large_grep(tool_input: dict, cwd: str, session_id: str) -> str | None:
    """Answer a plain, repo-wide literal Grep from the index once (an identical retry runs Grep).

    Only answers when the result means the same as Grep's: no path/glob/type/case/context options,
    a literal (regex-free) pattern, the cwd is the indexed root, and hits are matched case-sensitively.
    """
    pattern = str(tool_input.get("pattern") or "").strip()
    if not pattern or len(pattern) < 3 or any(c in _GREP_REGEX_CHARS for c in pattern):
        return None
    if any(tool_input.get(k) not in (None, "", False) for k in _GREP_SCOPE_KEYS):
        return None
    if str(tool_input.get("output_mode") or "files_with_matches") == "count":
        return None
    idx = workspace_index_summary(cwd)
    if not idx or normalize_workspace(cwd) != idx.get("source_root"):
        return None  # an index of a sub- or parent folder covers a different file set
    path = str(tool_input.get("path") or "").strip()
    if path and normalize_workspace(path) != idx.get("source_root"):
        return None
    try:
        from runtime import index_mcp

        res = index_mcp.dispatch_action("search_text", {"repo": idx["repo"], "query": pattern})
        matches = [m for m in (res.get("matches") or []) if pattern in str(m.get("text", ""))]
        if len(matches) < 25 or not _grep_answer_once(session_id, pattern):
            return None
        more = "at least " if res.get("truncated") else ""
        lines = [
            f"[Terser Index] First {min(25, len(matches))} of {more}{len(matches)} hits for `{pattern}` in "
            f"`{idx['repo']}` (index snapshot). Narrow with `path`/`glob`, or repeat this exact Grep to run it for real:"
        ]
        for m in matches[:25]:
            lines.append(f"  {m['file']}:{m['line']}: {m['text']}")
        return "\n".join(lines)
    except Exception:
        return None
