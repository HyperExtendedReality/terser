"""Terser native multi-language code outline (outline-first reads, Tier 2).

Produces a compact skeleton of a source file: an imports summary plus one line per
declaration with its exact line span. Declarations are found with per-language
patterns; block ends come from terser-index's ``_find_block_end_line`` so spans
match what ``get_symbol_source`` returns. Pure stdlib, fail-open (returns ``None``).
"""
from __future__ import annotations

import re
from pathlib import Path

_MAX_ROWS = 120
_SIG_CHARS = 110

_MODS = (
    r"(?:(?:export|default|public|private|protected|internal|static|async|abstract|final|sealed|"
    r"override|virtual|unsafe|extern|inline|const|readonly|partial|open|data|suspend|"
    r"pub(?:\([^)]*\))?|@\w+(?:\([^)]*\))?)\s+)*"
)

_C_LIKE_FUNC = re.compile(
    r"^\s*" + _MODS + r"(?:[\w:<>\[\],*&?]+\s+)+[*&]*(?P<name>[A-Za-z_~][\w:]*)\s*\([^;{}]*\)\s*(?:const\s*)?"
    r"(?:noexcept\s*)?(?:->\s*[\w:<>*&]+\s*)?(?:throws\s+[\w., ]+)?\s*\{?\s*$"
)
_NOT_FUNC_WORDS = frozenset({"if", "for", "while", "switch", "catch", "return", "else", "new", "sizeof", "using"})

_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "js": [
        re.compile(r"^\s*" + _MODS + r"(?P<kind>class|interface|enum|type|namespace|function\*?)\s+(?P<name>[\w$]+)"),
        re.compile(r"^\s*" + _MODS + r"(?:const|let|var)\s+(?P<name>[\w$]+)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*(?::[^=]+)?=>"),
        re.compile(r"^\s+" + _MODS + r"(?:get\s+|set\s+)?(?P<name>[\w$]+)\s*\([^)]*\)\s*(?::\s*[^{]+)?\{\s*$"),
    ],
    "py": [re.compile(r"^\s*(?:async\s+)?(?P<kind>def|class)\s+(?P<name>\w+)")],
    "go": [
        re.compile(r"^func\s+(?:\([^)]*\)\s*)?(?P<name>\w+)"),
        re.compile(r"^type\s+(?P<name>\w+)\s+(?P<kind>struct|interface)"),
    ],
    "rs": [re.compile(r"^\s*" + _MODS + r"(?P<kind>(?:fn|struct|enum|trait|impl|mod|type)\b|macro_rules!)\s*(?P<name>[\w<>:, ]*\w>?)")],
    "rb": [re.compile(r"^\s*(?P<kind>def|class|module)\s+(?P<name>[\w.:?!=]+)")],
    "lua": [re.compile(r"^\s*(?:local\s+)?function\s+(?P<name>[\w.:]+)")],
    "gd": [
        re.compile(r"^\s*(?:static\s+)?(?P<kind>func|class|class_name|signal|enum)\s+(?P<name>\w+)"),
    ],
    "swift": [re.compile(r"^\s*" + _MODS + r"(?P<kind>func|class|struct|enum|protocol|extension|actor|init)\b\s*(?P<name>[\w.]*)")],
    "kt": [re.compile(r"^\s*" + _MODS + r"(?P<kind>fun|class|object|interface|enum\s+class)\s+(?:<[^>]+>\s*)?(?P<name>[\w.]+)")],
    "php": [re.compile(r"^\s*" + _MODS + r"(?P<kind>function|class|interface|trait|enum)\s+(?P<name>\w+)")],
    "c_like": [
        re.compile(r"^\s*" + _MODS + r"(?P<kind>class|struct|interface|enum|record|namespace|union)\s+(?P<name>[\w.]+)"),
        _C_LIKE_FUNC,
    ],
    "zig": [re.compile(r"^\s*(?:pub\s+)?(?P<kind>fn|const)\s+(?P<name>\w+)\s*(?:\(|=\s*(?:struct|enum|union))")],
    "scala": [re.compile(r"^\s*" + _MODS + r"(?P<kind>def|class|object|trait|case\s+class)\s+(?P<name>\w+)")],
    "dart": [
        re.compile(r"^\s*(?:abstract\s+)?(?P<kind>class|mixin|extension|enum)\s+(?P<name>\w+)"),
        _C_LIKE_FUNC,
    ],
    "sql": [re.compile(r"(?i)^\s*create\s+(?:or\s+replace\s+)?(?P<kind>table|view|function|procedure|index|trigger)\s+(?:if\s+not\s+exists\s+)?(?P<name>[\w.\"]+)")],
}

_LANG_BY_EXT = {
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js", ".ts": "js", ".tsx": "js",
    ".py": "py", ".go": "go", ".rs": "rs", ".rb": "rb", ".lua": "lua", ".gd": "gd",
    ".swift": "swift", ".kt": "kt", ".kts": "kt", ".php": "php", ".zig": "zig", ".scala": "scala",
    ".dart": "dart", ".sql": "sql",
    ".c": "c_like", ".h": "c_like", ".cc": "c_like", ".cpp": "c_like", ".cxx": "c_like",
    ".hpp": "c_like", ".cs": "c_like", ".java": "c_like",
}

_IMPORT_RE = re.compile(
    r"^\s*(?:import\b|from\s+\S+\s+import\b|#include\b|using\s+[\w.]+\s*;|use\s+[\w:{}]+|require\(|"
    r"(?:const|let|var)\s+\w+\s*=\s*require\(|extends\s+\w|preload\(|package\s+[\w.]+)"
)


def is_supported(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in _LANG_BY_EXT


def _block_end(lines: list[str], idx: int, ext: str) -> int:
    try:
        from runtime.index_mcp import _find_block_end_line

        return int(_find_block_end_line(lines, idx, ext))
    except Exception:
        return idx + 1


def _match(lang: str, line: str) -> bool:
    for pat in _PATTERNS.get(lang, ()):
        m = pat.match(line)
        if not m:
            continue
        name = (m.groupdict().get("name") or "").strip()
        if pat is _C_LIKE_FUNC and (not name or name.split("::")[-1] in _NOT_FUNC_WORDS):
            continue
        return True
    return False


def outline(text: str, file_path: str) -> str | None:
    """Return a skeleton of ``text`` or ``None`` when no useful outline exists."""
    ext = Path(file_path).suffix.lower()
    lang = _LANG_BY_EXT.get(ext)
    if not lang or not text:
        return None
    lines = text.splitlines()
    imports = 0
    first_import = 0
    rows: list[str] = []
    in_block_comment = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if in_block_comment:
            in_block_comment = "*/" not in stripped
            continue
        if stripped.startswith("/*"):
            in_block_comment = "*/" not in stripped
            continue
        if stripped.startswith(("//", "#", "--", "*")) and not (lang == "c_like" and stripped.startswith("#include")):
            if not (lang == "py" and stripped.startswith("#!")):
                continue
        if _IMPORT_RE.match(line):
            imports += 1
            first_import = first_import or idx + 1
            continue
        if not _match(lang, line):
            continue
        end = _block_end(lines, idx, ext)
        span = f"L{idx + 1}-{end}" if end > idx + 1 else f"L{idx + 1}"
        indent = len(line) - len(line.lstrip(" \t"))
        pad = "  " * min(4, indent // 2 if "\t" not in line[:indent] else indent)
        sig = re.sub(r"\s{2,}", " ", stripped.rstrip("{").rstrip())
        if len(sig) > _SIG_CHARS:
            sig = sig[: _SIG_CHARS - 3] + "..."
        rows.append(f"{pad}{span}: {sig}")
        if len(rows) >= _MAX_ROWS:
            rows.append("... (more symbols follow)")
            break
    if len(rows) < 3:
        return None
    head = f"imports: {imports} line(s) from L{first_import}" if imports else ""
    return "\n".join(([head] if head else []) + rows)
