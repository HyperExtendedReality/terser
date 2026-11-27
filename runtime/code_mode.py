"""Terser Code Mode: local scripts and batch actions whose results, not inputs, reach context.

The agent describes the work; Terser does the reading locally and returns only what was
asked for. Four ``terser-index`` actions (reach them through ``order``):

``run_script``   Run a short Python (with the ``t`` helpers, see code_mode_helpers.py),
                 Node, Bash, or PowerShell script. Only stdout comes back, compressed;
                 large output is archived and searchable. Python file edits are staged:
                 ``apply=false`` returns a diff, ``apply=true`` writes all files or none.
``code_search``  Multi-pattern search in one call: files containing ALL of ``all``, ANY of
                 ``any``, and NONE of ``none``, returned as files, counts, or hits.
``code_edit``    A batch of exact or regex edits (or whole-symbol replacements) across
                 files, checked together and written all-or-nothing.
``code_query``   Pull selected values out of JSON/JSONL/TOML/CSV/INI files with a dotted
                 path (``dependencies.*``, ``items[0].name``) or a key outline.

Scripts run with the user's own permissions, like a shell command would. They are a
convenience for the agent, not a sandbox. Turn the actions off with
``terse config set code_mode=false`` (or ``TERSER_CODE_MODE=0``).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_MAX_CODE_CHARS = 64 * 1024
_FILTER_MIN_BYTES = 5120
_SERVE_MAX_CHARS = 12000
_FAIL_MAX_CHARS = 20000
_MARK = "[terser-code-mode]"
_EDITED_RE = re.compile(r"^\[terser-code-mode\] edited: (.+)$", re.M)
_READ_RE = re.compile(r"^\[terser-code-mode\] read_bytes=(\d+)\s*$", re.M)


def _enabled(env: dict[str, str] | None) -> bool:
    from runtime.config import load_config

    return bool(load_config(env).get("code_mode", True))


def _root_for(args: dict[str, Any], env: dict[str, str] | None) -> tuple[Path, str]:
    from runtime import index_mcp

    cwd = str(args.get("cwd") or "").strip()
    repo = str(args.get("repo") or "").strip()
    if not cwd and repo:
        resolved = index_mcp._resolve_repo_slug(repo, env=env)
        if resolved:
            cwd = str(resolved[1].get("source_root") or "")
    root = Path(cwd or os.getcwd()).expanduser()
    slug = repo if repo and not cwd else index_mcp.repo_slug_for_path(str(root), env=env)
    return root, slug


def _refresh_edited(paths: list[str], env: dict[str, str] | None) -> None:
    from runtime import index_mcp

    for p in paths:
        try:
            index_mcp.reindex_file(p, env=env)
        except Exception:
            continue


def _serve(text: str, *, label: str, intent: str, limit: int, env: dict[str, str] | None) -> tuple[str, str | None, bool]:
    """Return (served text, archive id, filtered?) for successful script output."""
    from runtime import context_sandbox, output_compressor

    raw_bytes = len(text.encode("utf-8", errors="replace"))
    if intent and raw_bytes > _FILTER_MIN_BYTES:
        aid = context_sandbox.store_output(text, command=label, env=env)
        matched = context_sandbox.query_output(aid, intent, limit=limit, env=env, require_hit=True) if aid else None
        if matched and len(matched) < len(text):
            return matched, aid, True
    served, stats = output_compressor.compress_output(
        text, command=label, max_chars=_SERVE_MAX_CHARS, host="code-mode", env=env,
    )
    return served, stats.get("archive_id"), False


def _interpreter(language: str) -> list[str] | None:
    lang = language.lower()
    if lang in ("python", "py"):
        return [sys.executable, "-I", "-X", "utf8"]
    if lang in ("node", "javascript", "js"):
        exe = shutil.which("node")
        return [exe] if exe else None
    if lang in ("bash", "sh", "shell"):
        exe = shutil.which("bash") or shutil.which("sh")
        return [exe] if exe else None
    if lang in ("powershell", "pwsh", "ps1"):
        exe = shutil.which("pwsh") or shutil.which("powershell")
        return [exe, "-NoProfile", "-NonInteractive", "-File"] if exe else None
    return None


_EXT = {"python": ".py", "py": ".py", "node": ".js", "javascript": ".js", "js": ".js",
        "bash": ".sh", "sh": ".sh", "shell": ".sh", "powershell": ".ps1", "pwsh": ".ps1", "ps1": ".ps1"}

_PY_BOOTSTRAP = """\
import sys, traceback
sys.path.insert(0, {plugin!r})
from runtime import code_mode_helpers as t
t._init({root!r}, {apply!r}, {repo!r})
src = open({script!r}, encoding="utf-8").read()
ns = {{"t": t, "__name__": "__main__"}}
try:
    exec(compile(src, "<script>", "exec"), ns)
except SystemExit as exc:
    if exc.code not in (None, 0):
        t._staged.clear()
        print("[terser-code-mode] script exited with " + str(exc.code) + "; nothing written", file=sys.stderr)
        raise
except t.CodeModeError as exc:
    t._staged.clear()
    print("[terser-code-mode] error: " + str(exc) + "; nothing written", file=sys.stderr)
    sys.exit(3)
except BaseException:
    t._staged.clear()
    traceback.print_exc()
    print("[terser-code-mode] nothing written", file=sys.stderr)
    sys.exit(1)
t._finish()
"""


def run_script(args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    from runtime.config import no_window_kwargs, record_savings, state_home

    if not _enabled(env):
        return {"error": "Code Mode is off (`terse config set code_mode=true` to enable)."}
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return {"error": "Provide `code` (the script source)."}
    if len(code) > _MAX_CODE_CHARS:
        return {"error": f"Script is over {_MAX_CODE_CHARS} characters."}
    language = str(args.get("language") or "python").strip().lower()
    interp = _interpreter(language)
    if interp is None:
        return {"error": f"No interpreter found for `{language}` (python, node, bash, powershell)."}
    root, slug = _root_for(args, env)
    if not root.is_dir():
        return {"error": f"Working directory not found: {root}"}
    apply = args.get("apply") is True
    timeout = max(1, min(120, int(args.get("timeout") or 30)))
    intent = str(args.get("intent") or "").strip()
    limit = max(1, min(15, int(args.get("limit") or 6)))

    work = state_home(env) / "code-mode" / os.urandom(6).hex()
    work.mkdir(parents=True, exist_ok=True)
    try:
        script = work / f"script{_EXT.get(language, '.txt')}"
        script.write_text(code, encoding="utf-8")
        if interp[0] == sys.executable:
            boot = _PY_BOOTSTRAP.format(plugin=str(_PLUGIN_ROOT), root=str(root), apply=apply, repo=slug, script=str(script))
            argv = [*interp, "-c", boot]
        else:
            argv = [*interp, str(script)]
        child_env = dict(os.environ if env is None else env)
        child_env.update({"PYTHONIOENCODING": "utf-8", "TERSER_CODE_MODE_ROOT": str(root)})
        try:
            proc = subprocess.run(
                argv, cwd=str(root), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout, env=child_env, **no_window_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return {"error": f"Script timed out after {timeout}s; nothing written.", "stdout": out[-4000:]}
        except OSError as exc:
            return {"error": f"Failed to start {argv[0]}: {exc}"}
    finally:
        shutil.rmtree(work, ignore_errors=True)

    stdout, stderr = proc.stdout or "", proc.stderr or ""
    edited = [m.strip() for m in _EDITED_RE.findall(stdout)]
    m_read = _READ_RE.search(stdout)
    read_bytes = int(m_read.group(1)) if m_read else 0
    stdout = _READ_RE.sub("", stdout).rstrip() + ("\n" if stdout.strip() else "")
    label = f"code_mode:{language}"
    result: dict[str, Any] = {"returncode": proc.returncode, "language": language, "root": str(root)}
    if edited:
        _refresh_edited(edited, env)
        result["edited"] = edited
    if proc.returncode != 0:
        from runtime import context_sandbox

        both = stdout + ("\n--- stderr ---\n" + stderr if stderr else "")
        if len(both) > _FAIL_MAX_CHARS:
            aid = context_sandbox.store_output(both, command=label, env=env)
            result["archive_id"] = aid
            both = both[:_FAIL_MAX_CHARS // 2] + "\n... [middle omitted] ...\n" + both[-_FAIL_MAX_CHARS // 2:]
        result["output"] = both
        return result
    served, aid, filtered = _serve(stdout, label=label, intent=intent, limit=limit, env=env)
    result.update({"output": served, "filtered": filtered})
    if aid:
        result["archive_id"] = aid
    if stderr.strip():
        result["stderr"] = stderr[-4000:]
    if read_bytes > len(served):
        record_savings("code_mode", read_bytes, len(served), host=os.environ.get("TERSER_CLIENT_ID", "mcp"), detail=language, env=env)
    result["read_bytes"] = read_bytes
    return result


# ---------------------------------------------------------------------------
# In-process actions (no subprocess): search, edit, query
# ---------------------------------------------------------------------------

def _helpers(root: Path, apply: bool, slug: str) -> Any:
    from runtime import code_mode_helpers as t

    t._init(str(root), apply, slug)
    return t


def _as_list(val: Any) -> list[str]:
    if val is None:
        return []
    if isinstance(val, str):
        return [val]
    return [str(v) for v in val if str(v)]


def code_search(args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    if not _enabled(env):
        return {"error": "Code Mode is off."}
    root, slug = _root_for(args, env)
    t = _helpers(root, False, slug)
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    try:
        all_rx = [re.compile(p, flags) for p in _as_list(args.get("all") or args.get("pattern"))]
        any_rx = [re.compile(p, flags) for p in _as_list(args.get("any"))]
        none_rx = [re.compile(p, flags) for p in _as_list(args.get("none"))]
    except re.error as exc:
        return {"error": f"Bad regex: {exc}"}
    if not (all_rx or any_rx):
        return {"error": "Provide `all` and/or `any` patterns (`none` excludes files)."}
    mode = str(args.get("mode") or "files")
    max_hits = max(1, min(500, int(args.get("max_hits") or 120)))
    globs = _as_list(args.get("glob")) or ["**/*"]
    rows: list[str] = []
    files_hit = 0
    hits = 0
    for f in t.files(globs):
        try:
            text = t._read_text(root / f)
        except Exception:
            continue
        if "\0" in text[:4096]:
            continue
        if any(not rx.search(text) for rx in all_rx):
            continue
        if any_rx and not any(rx.search(text) for rx in any_rx):
            continue
        if any(rx.search(text) for rx in none_rx):
            continue
        files_hit += 1
        show_rx = all_rx + any_rx
        if mode == "files":
            rows.append(f)
        elif mode == "count":
            n = sum(len(rx.findall(text)) for rx in show_rx)
            rows.append(f"{f}\t{n}")
        else:
            rows.append(f"{f}:")
            for no, line in enumerate(text.splitlines(), start=1):
                if any(rx.search(line) for rx in show_rx):
                    rows.append(f"  {no}: {line.strip()[:200]}")
                    hits += 1
                    if hits >= max_hits:
                        break
        if hits >= max_hits or len(rows) >= 2000:
            rows.append("... [limit reached]")
            break
    from runtime.config import record_savings

    if t._bytes_read:
        body_len = sum(len(r) + 1 for r in rows)
        if t._bytes_read > body_len:
            record_savings("code_mode", t._bytes_read, body_len, host=os.environ.get("TERSER_CLIENT_ID", "mcp"), detail="code_search", env=env)
    return {"code_search": f"{files_hit} file(s) matched ({mode}):\n" + "\n".join(rows) if rows else "0 files matched."}


def code_edit(args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    if not _enabled(env):
        return {"error": "Code Mode is off."}
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        return {"error": "Provide `edits`: a list of {path, old, new} | {path|glob, pattern, repl} | {symbol_id, source}."}
    root, slug = _root_for(args, env)
    apply = args.get("apply") is True
    t = _helpers(root, apply, slug)
    notes: list[str] = []
    try:
        for i, e in enumerate(edits, start=1):
            if not isinstance(e, dict):
                raise t.CodeModeError(f"edit {i} is not an object")
            if "symbol_id" in e:
                t.edit_symbol(str(e["symbol_id"]), str(e.get("source") or ""))
            elif "old" in e:
                t.replace(str(e.get("path") or ""), str(e["old"]), str(e.get("new") or ""), int(e.get("count", 1)))
            elif "pattern" in e:
                targets = [str(e["path"])] if e.get("path") else t.files(_as_list(e.get("glob")) or ["**/*"])
                total = sum(t.sub(p, str(e["pattern"]), str(e.get("repl") or ""), int(e.get("count", 0))) for p in targets)
                if total == 0 and not e.get("allow_none"):
                    raise t.CodeModeError(f"edit {i}: pattern matched nothing: {e['pattern']!r}")
                notes.append(f"edit {i}: {total} replacement(s)")
            elif "content" in e:
                t.write(str(e.get("path") or ""), str(e["content"]))
            else:
                raise t.CodeModeError(f"edit {i}: needs old/new, pattern/repl, symbol_id/source, or content")
    except (t.CodeModeError, OSError, UnicodeDecodeError, re.error, ValueError, KeyError) as exc:
        return {"error": f"{exc}. Nothing was written."}
    import io

    buf = io.StringIO()
    edited = t._finish(buf)
    text = _READ_RE.sub("", buf.getvalue()).strip()
    if edited:
        _refresh_edited(edited, env)
    if notes:
        text = "\n".join(notes) + "\n" + text
    return {"code_edit": text or "No changes.", "edited": edited}


def _walk(node: Any, parts: list[str]) -> list[tuple[str, Any]]:
    if not parts:
        return [("", node)]
    head, rest = parts[0], parts[1:]
    out: list[tuple[str, Any]] = []
    m = re.fullmatch(r"([^\[\]]*)((?:\[\d+\]|\[\*\])*)", head)
    key, idxs = (m.group(1), re.findall(r"\[(\d+|\*)\]", m.group(2))) if m else (head, [])
    cur: list[tuple[str, Any]] = [(key, node)]
    if key:
        nxt = []
        for label, n in cur:
            if key == "*" and isinstance(n, dict):
                nxt.extend((k, v) for k, v in n.items())
            elif key == "*" and isinstance(n, list):
                nxt.extend((str(i), v) for i, v in enumerate(n))
            elif isinstance(n, dict) and key in n:
                nxt.append((key, n[key]))
        cur = nxt
    for ix in idxs:
        nxt = []
        for label, n in cur:
            if not isinstance(n, list):
                continue
            if ix == "*":
                nxt.extend((f"{label}[{i}]", v) for i, v in enumerate(n))
            elif int(ix) < len(n):
                nxt.append((f"{label}[{ix}]", n[int(ix)]))
        cur = nxt
    for label, n in cur:
        for sub_label, val in _walk(n, rest):
            out.append((label + ("." + sub_label if sub_label else ""), val))
    return out


def _outline(node: Any, depth: int, max_depth: int, prefix: str = "") -> list[str]:
    lines: list[str] = []
    if isinstance(node, dict):
        for k, v in list(node.items())[:60]:
            kind = type(v).__name__
            extra = f" ({len(v)})" if isinstance(v, (list, dict)) else ""
            lines.append(f"{prefix}{k}: {kind}{extra}")
            if depth < max_depth and isinstance(v, (dict, list)):
                lines.extend(_outline(v, depth + 1, max_depth, prefix + "  "))
        if len(node) > 60:
            lines.append(f"{prefix}... {len(node) - 60} more keys")
    elif isinstance(node, list) and node:
        lines.append(f"{prefix}[{len(node)} x {type(node[0]).__name__}]")
        if depth < max_depth and isinstance(node[0], (dict, list)):
            lines.extend(_outline(node[0], depth + 1, max_depth, prefix + "  "))
    return lines


def code_query(args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    if not _enabled(env):
        return {"error": "Code Mode is off."}
    path = str(args.get("path") or "").strip()
    if not path:
        return {"error": "Provide `path`."}
    root, slug = _root_for(args, env)
    t = _helpers(root, False, slug)
    p = t._resolve(path)
    fmt = str(args.get("format") or p.suffix.lstrip(".")).lower()
    try:
        if fmt in ("json", "jsonc", "map", "lock") or p.name.endswith(".json"):
            data: Any = t.json(path)
        elif fmt in ("jsonl", "ndjson"):
            data = [json.loads(ln) for ln in t.lines(path) if ln.strip()]
        elif fmt == "toml":
            data = t.toml(path)
        elif fmt in ("csv", "tsv"):
            data = t.csv_rows(path, **({"delimiter": "\t"} if fmt == "tsv" else {}))
        elif fmt in ("ini", "cfg", "conf"):
            cp = t.ini(path)
            data = {s: dict(cp[s]) for s in cp.sections()}
        else:
            return {"error": f"Unsupported format `{fmt}` (json, jsonl, toml, csv, tsv, ini)."}
    except Exception as exc:
        return {"error": f"Could not parse {path}: {exc}"}
    if args.get("outline") or not args.get("select"):
        depth = max(0, min(4, int(args.get("depth") or 1)))
        lines = _outline(data, 0, depth)
        return {"code_query": f"{t._rel(p)} ({fmt}) outline:\n" + "\n".join(lines or [f"{type(data).__name__}"])}
    select = str(args["select"]).strip().lstrip("$.")
    matches = _walk(data, [s for s in re.split(r"\.(?![^\[]*\])", select) if s]) if select else [("", data)]
    limit = max(1, min(500, int(args.get("limit") or 100)))
    rows = []
    for label, val in matches[:limit]:
        shown = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False, separators=(",", ":"), default=str)
        if len(shown) > 2000:
            shown = shown[:2000] + f"...[+{len(shown) - 2000} chars]"
        rows.append(f"{label or select}\t{shown}")
    more = f"\n... {len(matches) - limit} more" if len(matches) > limit else ""
    return {"code_query": (f"{len(matches)} value(s) for `{select}` in {t._rel(p)}:\n" + "\n".join(rows) + more) if rows else f"No values for `{select}` in {t._rel(p)}."}


def dispatch(action: str, args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    if action == "run_script":
        return run_script(args, env=env)
    if action == "code_search":
        return code_search(args, env=env)
    if action == "code_edit":
        return code_edit(args, env=env)
    if action == "code_query":
        return code_query(args, env=env)
    return None


def format_response(action: str, payload: dict[str, Any]) -> str | None:
    if action == "run_script" and "returncode" in payload:
        head = f"exit={payload['returncode']} {payload.get('language', '')}"
        if payload.get("filtered"):
            head += " filtered"
        if payload.get("archive_id"):
            head += f" archive_id={payload['archive_id']}"
        if payload.get("read_bytes"):
            head += f" read={payload['read_bytes']}B"
        parts = [head, str(payload.get("output") or "").rstrip()]
        if payload.get("stderr"):
            parts.append("--- stderr ---\n" + str(payload["stderr"]).rstrip())
        return "\n".join(p for p in parts if p)
    for key in ("code_search", "code_edit", "code_query"):
        if action == key and key in payload:
            return str(payload[key])
    return None


def edited_paths_from_output(text: str) -> list[str]:
    """Paths a Code Mode call wrote, parsed from its marker lines (for hook-side cache refresh)."""
    return [m.strip() for m in _EDITED_RE.findall(text or "")]
