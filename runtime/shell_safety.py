"""Terser native shell-command safety gates for output-compression wrapping.

Wrapping a command only reroutes its stdout/stderr through Terser's compressor; the
command itself still runs exactly as written. These gates keep that true:

* ``has_dangerous_chars`` rejects anything a wrapper could change the meaning of
  (pipes, redirects, substitution, background jobs, multiple statements).
* ``is_whitelisted`` allows only non-interactive commands whose output Terser knows
  how to compress. Anything that may open an editor, pager, or prompt is excluded.
* ``is_auto_approvable`` is a stricter read-only subset (opt-in ``auto_approve_readonly``).
"""
from __future__ import annotations

import re
import shlex

_DANGEROUS_RE = re.compile(r"[;|&<>`\n\r]|\$\(|\$\{|<\(|>\(")

# command -> allowed first subcommands (None = any arguments are fine)
_WHITELIST: dict[str, frozenset[str] | None] = {
    "git": frozenset({"status", "diff", "log", "show", "branch", "ls-files", "grep", "blame", "fetch", "pull",
                      "push", "remote", "tag", "shortlog", "describe", "rev-parse", "stash", "clone", "cherry"}),
    "cargo": frozenset({"build", "check", "test", "clippy", "fmt", "run", "nextest", "tree", "metadata", "doc", "bench"}),
    "go": frozenset({"build", "test", "vet", "list", "mod", "run", "env", "version"}),
    "npm": frozenset({"install", "i", "ci", "test", "run", "ls", "list", "outdated", "audit", "build", "view"}),
    "pnpm": frozenset({"install", "i", "add", "test", "run", "ls", "list", "outdated", "audit", "build"}),
    "yarn": frozenset({"install", "add", "test", "run", "list", "outdated", "audit", "build"}),
    "bun": frozenset({"install", "i", "add", "test", "run", "build", "pm"}),
    "pip": frozenset({"install", "list", "show", "freeze", "check", "download"}),
    "pip3": frozenset({"install", "list", "show", "freeze", "check", "download"}),
    "uv": frozenset({"pip", "sync", "lock", "run", "tree", "build"}),
    "python": frozenset({"-m"}), "python3": frozenset({"-m"}), "py": frozenset({"-m"}),
    "pytest": None, "tsc": None, "eslint": None, "ruff": None, "mypy": None, "pyright": None,
    "flake8": None, "jest": None, "vitest": None, "mocha": None, "prettier": None, "biome": None,
    "docker": frozenset({"build", "ps", "images", "logs", "inspect", "compose", "buildx", "version", "info"}),
    "kubectl": frozenset({"get", "describe", "logs", "top", "version", "explain"}),
    "terraform": frozenset({"plan", "validate", "fmt", "init", "show", "output", "providers"}),
    "helm": frozenset({"list", "status", "template", "lint", "version"}),
    "gradle": None, "gradlew": None, "mvn": None, "mvnw": None,
    "dotnet": frozenset({"build", "test", "restore", "list", "--info", "--version"}),
    "make": None, "cmake": None, "ninja": None, "msbuild": None,
    "ls": None, "dir": None, "tree": None, "find": None, "fd": None, "rg": None, "grep": None,
    "wc": None, "du": None, "df": None, "cat": None, "head": None, "tail": None, "stat": None,
    "godot": None, "godot4": None,
}
_PYTHON_MODULES = frozenset({"pytest", "unittest", "mypy", "ruff", "pip", "pyright", "black", "flake8", "compileall"})
_NEVER_ENDS = frozenset({"--watch", "--watchAll", "--interactive", "--follow", "--serve"})
_WATCH_SHORT = frozenset({"jest", "vitest", "tsc", "mocha"})
_FOLLOW_SHORT = frozenset({"tail", "docker", "kubectl"})
_FIND_UNSAFE = frozenset({"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"})

_READONLY: dict[str, frozenset[str] | None] = {
    "git": frozenset({"status", "diff", "log", "show", "ls-files", "grep", "blame", "shortlog", "describe", "rev-parse"}),
    "ls": None, "dir": None, "tree": None, "rg": None, "grep": None, "wc": None, "du": None, "df": None,
    "cat": None, "head": None, "tail": None, "stat": None, "find": None, "fd": None,
}


def _tokens(command: str) -> list[str]:
    try:
        toks = shlex.split(command, posix=True)
    except ValueError:
        return []
    if toks:
        toks[0] = toks[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe").removesuffix(".cmd").removesuffix(".bat")
    return toks


def has_dangerous_chars(command: str) -> bool:
    return bool(_DANGEROUS_RE.search(command or ""))


def _first_sub(toks: list[str]) -> str:
    rest = toks[1:]
    if toks[0] == "git":
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in ("-C", "-c") else 1
        return rest[i] if i < len(rest) else ""
    if toks[0] in ("python", "python3", "py", "dotnet"):
        return rest[0] if rest else ""
    return next((a for a in rest if not a.startswith("-")), "")


def _allowed(toks: list[str], table: dict[str, frozenset[str] | None]) -> bool:
    if not toks or toks[0] not in table:
        return False
    if "=" in toks[0] or _NEVER_ENDS.intersection(toks[1:]):
        return False
    if toks[0] in _WATCH_SHORT and "-w" in toks[1:]:
        return False
    if toks[0] in _FOLLOW_SHORT and ("-f" in toks[1:] or "-F" in toks[1:]):
        return False
    if toks[0] == "find" and _FIND_UNSAFE.intersection(toks):
        return False
    subs = table[toks[0]]
    if subs is None:
        return True
    sub = _first_sub(toks)
    if sub not in subs:
        return False
    if toks[0] in ("python", "python3", "py"):
        return len(toks) >= 3 and toks[2] in _PYTHON_MODULES
    if toks[0] == "git" and sub == "stash":
        return "list" in toks or "show" in toks
    if toks[0] == "git" and sub in ("branch", "tag", "remote"):
        return all(t.startswith("-") for t in toks[toks.index(sub) + 1:]) or sub == "remote" and "-v" in toks
    return True


def is_whitelisted(command: str) -> bool:
    if has_dangerous_chars(command):
        return False
    return _allowed(_tokens(command), _WHITELIST)


def is_auto_approvable(command: str) -> bool:
    if has_dangerous_chars(command):
        return False
    toks = _tokens(command)
    if not _allowed(toks, _READONLY):
        return False
    # Read-only commands must not write files through output flags.
    return not any(t in ("-o", "--output", "--output-file") or t.startswith("--output=") for t in toks)
