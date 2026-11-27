"""User-defined command filters and ``.contextignore`` for Terser.

Command filters
---------------
Add or exclude compression rules without changing code. Terser reads, in order:

* ``~/.terser/command-filters.toml`` (user level)
* ``<workspace>/.terser/command-filters.toml`` (project level)

    [[filter]]
    name = "mytool"                    # label used in the omitted-lines note
    match = "^mytool\\s+(build|sync)"  # regex matched against the command
    drop = ["^\\s*\\[info\\]", "^Downloading "]   # line regexes to drop
    keep = ["^SUMMARY"]                # optional: lines always kept
    handler = "pytest"                 # optional: also run a built-in handler

    [exclude]
    commands = ["kubectl logs *", "cat *.json"]   # globs: never compress these

Safety gate, the same one built-in handlers follow: a line matching Terser's error or
warning signal pattern is never dropped, a failed non-test command is left whole, and
every drop is counted in a ``[terser: ...]`` note. Bad regexes are skipped.

``.contextignore``
------------------
Gitignore-style patterns for files that must never be read into context. Read
from ``<workspace>/.contextignore`` and ``~/.terser/.contextignore``. A matching Read
is denied with the matching pattern named. ``!pattern`` re-includes a path.

Both files are optional. When neither exists every function here returns quickly and
changes nothing. Stdlib only. Parsed files are cached on ``(path, mtime, size)``.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Callable

FILTERS_NAME = "command-filters.toml"
IGNORE_NAME = ".contextignore"

_SIGNAL_RE = re.compile(
    r"(?i)(error|fail|panic|exception|traceback|warning|warn\b|fatal|denied|not found|"
    r"cannot|could not|unable|segfault|assert|conflict|refused|timed? ?out|E\d{3,})"
)

_cache: dict[str, tuple[tuple[int, int], Any]] = {}


def _home(env: dict[str, str] | None) -> Path:
    from runtime.config import state_home

    return state_home(env)


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _cached(path: Path, loader: Callable[[str], Any]) -> Any:
    key = _stat_key(path)
    if key is None:
        return None
    hit = _cache.get(str(path))
    if hit and hit[0] == key:
        return hit[1]
    try:
        value = loader(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        value = None
    _cache[str(path)] = (key, value)
    return value


# ---------------------------------------------------------------------------
# Minimal TOML subset (tables, arrays of tables, strings, string arrays, bools,
# ints). ``tomllib`` is used when available (3.11+).
# ---------------------------------------------------------------------------

def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"""') or raw.startswith("'''"):
        raise ValueError("multi-line strings are not supported")
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        body = raw[1:-1]
        return re.sub(r'\\(["\\/bfnrt])', lambda m: {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}.get(m.group(1), m.group(1)), body)
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"[+-]?\d+", raw):
        return int(raw)
    raise ValueError(f"unsupported value: {raw[:40]}")


def _split_array(body: str) -> list[str]:
    items, cur, quote, i = [], "", "", 0
    while i < len(body):
        ch = body[i]
        if quote:
            cur += ch
            if ch == "\\" and quote == '"' and i + 1 < len(body):
                cur += body[i + 1]
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            cur += ch
        elif ch == ",":
            if cur.strip():
                items.append(cur)
            cur = ""
        elif ch == "#":
            while i < len(body) and body[i] != "\n":
                i += 1
            continue
        else:
            cur += ch
        i += 1
    if cur.strip():
        items.append(cur)
    return items


def _strip_comment(line: str) -> str:
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote and line[i - 1] != "\\":
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def parse_toml(text: str) -> dict[str, Any]:
    try:
        import tomllib  # type: ignore[import-not-found]

        return tomllib.loads(text)
    except ImportError:
        pass
    root: dict[str, Any] = {}
    cur: dict[str, Any] = root
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = _strip_comment(lines[i]).strip()
        i += 1
        if not line:
            continue
        m = re.fullmatch(r"\[\[\s*([A-Za-z0-9_.-]+)\s*\]\]", line)
        if m:
            *parents, last = m.group(1).split(".")
            node = root
            for part in parents:
                node = node.setdefault(part, {})
            cur = {}
            node.setdefault(last, []).append(cur)
            continue
        m = re.fullmatch(r"\[\s*([A-Za-z0-9_.-]+)\s*\]", line)
        if m:
            node = root
            for part in m.group(1).split("."):
                nxt = node.setdefault(part, {})
                node = nxt[-1] if isinstance(nxt, list) else nxt
            cur = node
            continue
        key, sep, val = line.partition("=")
        if not sep:
            raise ValueError(f"bad line: {line[:40]}")
        key = key.strip().strip('"')
        val = val.strip()
        if val.startswith("["):
            while val.count("[") > val.count("]") and i < len(lines):
                val += "\n" + lines[i]
                i += 1
            body = val.strip()[1:-1]
            cur[key] = [_parse_scalar(x) for x in _split_array(body)]
        else:
            cur[key] = _parse_scalar(val)
    return root


# ---------------------------------------------------------------------------
# Command filters
# ---------------------------------------------------------------------------

def _compile_filters(text: str, scope: str) -> dict[str, Any]:
    data = parse_toml(text)
    filters: list[dict[str, Any]] = []
    for raw in data.get("filter") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("match"), str):
            continue
        try:
            match_re = re.compile(raw["match"])
        except re.error:
            continue
        drop, keep = [], []
        for src, dst in ((raw.get("drop"), drop), (raw.get("keep"), keep)):
            for pat in src if isinstance(src, list) else []:
                try:
                    dst.append(re.compile(str(pat)))
                except re.error:
                    continue
        filters.append({
            "name": str(raw.get("name") or raw["match"])[:40],
            "match": match_re,
            "drop": drop,
            "keep": keep,
            "handler": str(raw.get("handler") or ""),
            "scope": scope,
        })
    excl = data.get("exclude") if isinstance(data.get("exclude"), dict) else {}
    globs = [str(g) for g in (excl.get("commands") or []) if str(g).strip()]
    return {"filters": filters, "exclude": globs}


def filter_files(workspace: str | None, env: dict[str, str] | None = None) -> list[tuple[Path, str]]:
    out = [(_home(env) / FILTERS_NAME, "user")]
    if workspace:
        out.append((Path(workspace) / ".terser" / FILTERS_NAME, "project"))
    return out


def load_filters(workspace: str | None, env: dict[str, str] | None = None) -> dict[str, Any]:
    merged: dict[str, Any] = {"filters": [], "exclude": []}
    for path, scope in filter_files(workspace, env):
        got = _cached(path, lambda t, s=scope: _compile_filters(t, s))
        if got:
            merged["filters"].extend(got["filters"])
            merged["exclude"].extend(got["exclude"])
    return merged


def _norm_command(command: str) -> str:
    return " ".join(str(command or "").split())


def is_excluded(command: str, workspace: str | None = None, env: dict[str, str] | None = None) -> bool:
    cmd = _norm_command(command)
    if not cmd:
        return False
    return any(fnmatch.fnmatchcase(cmd, g) for g in load_filters(workspace, env)["exclude"])


def matching_filter(command: str, workspace: str | None = None, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    cmd = _norm_command(command)
    if not cmd:
        return None
    for f in load_filters(workspace, env)["filters"]:
        if f["match"].search(cmd):
            return f
    return None


def user_filter_allows_wrap(command: str, env: dict[str, str] | None = None) -> bool:
    """True when a user-level (not project-level) filter names this command.

    The user wrote that rule, so the command may be wrapped for compression even when it
    is not on the built-in whitelist. Project files never widen the whitelist.
    """
    f = matching_filter(command, None, env)
    return bool(f and f["scope"] == "user")


def apply_filters(
    command: str,
    text: str,
    *,
    returncode: int = 0,
    workspace: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Apply the first matching user filter. Returns ``text`` unchanged when none applies."""
    if not text or not command:
        return text
    try:
        f = matching_filter(command, workspace, env)
        if not f:
            return text
        from runtime import command_handlers

        handler = command_handlers.HANDLERS.get(f["handler"]) if f["handler"] else None
        is_test = f["handler"] in command_handlers.TEST_HANDLER_NAMES
        if returncode != 0 and not is_test:
            return text
        cur = text
        if f["drop"]:
            kept, dropped = [], 0
            for line in cur.splitlines():
                if (
                    any(p.search(line) for p in f["drop"])
                    and not _SIGNAL_RE.search(line)
                    and not any(p.search(line) for p in f["keep"])
                ):
                    dropped += 1
                    continue
                kept.append(line)
            if dropped:
                kept.append(f"[terser: {dropped} {f['name']} line(s) omitted by command-filters.toml]")
                cur = "\n".join(kept) + "\n"
        if handler is not None:
            try:
                out = handler(cur)
                if out and len(out) < len(cur):
                    cur = out
            except Exception:
                pass
        return cur if len(cur) < len(text) else text
    except Exception:
        return text


def filters_present(workspace: str | None, env: dict[str, str] | None = None) -> bool:
    return any(p.is_file() for p, _ in filter_files(workspace, env))


# ---------------------------------------------------------------------------
# .contextignore
# ---------------------------------------------------------------------------

def _glob_to_regex(pat: str) -> str:
    out, i = "", 0
    while i < len(pat):
        ch = pat[i]
        if ch == "*":
            if pat[i:i + 3] == "**/":
                out += "(?:.*/)?"
                i += 3
                continue
            if pat[i:i + 2] == "**":
                out += ".*"
                i += 2
                continue
            out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        elif ch == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out += re.escape(ch)
            else:
                out += pat[i:j + 1].replace("!", "^", 1) if pat[i + 1:i + 2] == "!" else pat[i:j + 1]
                i = j
        else:
            out += re.escape(ch)
        i += 1
    return out


def _compile_ignore(text: str) -> list[tuple[re.Pattern[str], bool, str]]:
    rules = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        pat = line[1:] if negate else line
        dir_only = pat.endswith("/")
        pat = pat.rstrip("/")
        anchored = pat.startswith("/") or "/" in pat
        pat = pat.lstrip("/")
        if not pat:
            continue
        body = _glob_to_regex(pat)
        prefix = "^" if anchored else r"^(?:.*/)?"
        suffix = "/.*$" if dir_only else "(?:/.*)?$"
        try:
            flags = re.IGNORECASE if os.name == "nt" else 0
            rules.append((re.compile(prefix + body + suffix, flags), negate, line))
        except re.error:
            continue
    return rules


def ignore_files(workspace: str | None, env: dict[str, str] | None = None) -> list[Path]:
    out = []
    if workspace:
        out.append(Path(workspace) / IGNORE_NAME)
    out.append(_home(env) / IGNORE_NAME)
    return out


def _rel_for(path: str, workspace: str | None) -> str:
    p = str(path or "").replace("\\", "/")
    if workspace:
        ws = str(workspace).replace("\\", "/").rstrip("/")
        if os.name == "nt":
            if p.lower().startswith(ws.lower() + "/"):
                return p[len(ws) + 1:]
        elif p.startswith(ws + "/"):
            return p[len(ws) + 1:]
    return p.lstrip("/")


def ignored_by(path: str, workspace: str | None = None, env: dict[str, str] | None = None) -> str | None:
    """Return the ``.contextignore`` pattern that blocks ``path``, or ``None``."""
    if not path:
        return None
    rel = _rel_for(path, workspace)
    verdict: str | None = None
    # Global rules first, then project rules, so a project `!pattern` can re-include.
    for f in reversed(ignore_files(workspace, env)):
        rules = _cached(f, _compile_ignore)
        for rx, negate, line in rules or []:
            if rx.match(rel):
                verdict = None if negate else f"{line} ({f})"
    return verdict


def ignore_present(workspace: str | None, env: dict[str, str] | None = None) -> bool:
    return any(p.is_file() for p in ignore_files(workspace, env))


def deny_reason(path: str, pattern: str) -> str:
    return (
        f"[Terser] `{path}` matches .contextignore pattern `{pattern}`, so it is not read into context. "
        "If the user needs it, ask them to remove the pattern."
    )
