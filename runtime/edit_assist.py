"""Terser — Edit-miss recovery.

When an Edit / MultiEdit fails because `old_string` is missing or ambiguous, the usual
recovery is a full re-read of the file. This module instead returns the exact current
text of the closest matching region (with line numbers) or the line numbers of every
ambiguous occurrence, so the model can retry the edit straight away.

It only adds information; nothing the model would otherwise see is removed.
Pure Python stdlib, fail-open.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

_MAX_FILE_BYTES = 1024 * 1024
_MAX_SHOWN_LINES = 60
_CONTEXT_LINES = 2
_MIN_RATIO = 0.5
_MAX_CANDIDATES = 300

_NOT_FOUND_RE = re.compile(r"not found|could not find|no match", re.I)
_MULTI_RE = re.compile(r"found\s+(\d+)\s+matches|multiple (?:matches|occurrences)|not unique", re.I)


def _numbered(lines: list[str], start: int, end: int) -> str:
    """Lines start..end (1-based, inclusive) in the host Read tool's `N<TAB>text` layout."""
    return "\n".join(f"{ln:>6}\t{lines[ln - 1]}" for ln in range(start, end + 1))


def _occurrence_lines(text: str, needle: str) -> list[int]:
    out: list[int] = []
    pos = text.find(needle)
    while pos >= 0 and len(out) < 50:
        out.append(text.count("\n", 0, pos) + 1)
        pos = text.find(needle, pos + 1)
    return out


def _best_window(file_lines: list[str], old_lines: list[str]) -> tuple[int, int, float] | None:
    """Best (start, end, ratio) window (1-based, inclusive) resembling `old_lines`."""
    n = len(old_lines)
    if not n or not file_lines:
        return None
    wanted: dict[str, list[int]] = {}
    for k, ln in enumerate(old_lines):
        key = ln.strip()
        if len(key) >= 4:
            wanted.setdefault(key, []).append(k)
    if not wanted:
        return None
    starts: set[int] = set()
    for i, ln in enumerate(file_lines):
        ks = wanted.get(ln.strip())
        if ks:
            for k in ks:
                st = i - k
                if 0 <= st < len(file_lines):
                    starts.add(st)
        if len(starts) >= _MAX_CANDIDATES:
            break
    if not starts:
        return None
    target = "\n".join(old_lines)
    best: tuple[int, int, float] | None = None
    for st in sorted(starts):
        window = file_lines[st:st + n]
        ratio = difflib.SequenceMatcher(None, "\n".join(window), target, autojunk=False).ratio()
        if best is None or ratio > best[2]:
            best = (st + 1, min(len(file_lines), st + n), ratio)
    if best is None or best[2] < _MIN_RATIO:
        return None
    return best


def _difference_note(window: list[str], old_lines: list[str]) -> str:
    if [w.strip() for w in window] == [o.strip() for o in old_lines]:
        if any("\t" in w[: len(w) - len(w.lstrip())] for w in window) and not any("\t" in o for o in old_lines):
            return "differs only in indentation: the file indents with tabs"
        return "differs only in leading/trailing whitespace or indentation"
    if [re.sub(r"\s+", "", w) for w in window] == [re.sub(r"\s+", "", o) for o in old_lines]:
        return "differs only in whitespace"
    return "closest match; content differs"


def edit_failure_hint(
    file_path: str,
    old_string: str,
    error: str,
    *,
    replace_all: bool = False,
    edit_index: int | None = None,
) -> tuple[str, tuple[int, int] | None] | None:
    """Return (hint, shown_line_range) for a failed edit, or None when nothing useful applies."""
    if not file_path or not isinstance(old_string, str) or not old_string.strip():
        return None
    target = Path(file_path)
    try:
        if not target.is_file() or target.stat().st_size > _MAX_FILE_BYTES:
            return None
        raw = target.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    needle = old_string.replace("\r\n", "\n")
    name = target.name
    which = f" (edit #{edit_index + 1})" if edit_index is not None else ""
    file_lines = text.split("\n")
    if len(file_lines) > 1 and file_lines[-1] == "":
        file_lines.pop()  # trailing newline is not a line of its own

    occurrences = _occurrence_lines(text, needle)
    if len(occurrences) > 1 and not replace_all and (_MULTI_RE.search(error or "") or not _NOT_FOUND_RE.search(error or "")):
        shown = ", ".join(f"L{n}" for n in occurrences[:12])
        more = f" (+{len(occurrences) - 12} more)" if len(occurrences) > 12 else ""
        first_line = needle.split("\n", 1)[0].strip()[:80]
        hint = (
            f"[Terser] `old_string`{which} matches {len(occurrences)} places in `{name}`: {shown}{more} "
            f"(first line `{first_line}`). Add surrounding lines to make it unique, or set replace_all."
        )
        return hint, None

    if occurrences:
        return None  # The string exists; the failure has another cause.

    old_lines = needle.split("\n")
    while old_lines and not old_lines[-1].strip():
        old_lines.pop()
    best = _best_window(file_lines, old_lines)
    if best is None:
        hint = (
            f"[Terser] `old_string`{which} does not occur in `{name}` and no similar region was found; "
            "the code may have moved or been removed. Search for a distinctive identifier before re-reading."
        )
        return hint, None
    start, end, _ratio = best
    note = _difference_note(file_lines[start - 1:end], old_lines)
    show_s = max(1, start - _CONTEXT_LINES)
    show_e = min(len(file_lines), end + _CONTEXT_LINES)
    truncated = ""
    if show_e - show_s + 1 > _MAX_SHOWN_LINES:
        show_e = show_s + _MAX_SHOWN_LINES - 1
        truncated = f"\n... (region continues to L{end + _CONTEXT_LINES})"
    hint = (
        f"[Terser] `old_string`{which} was not found in `{name}`. Lines {start}-{end} {note}. "
        f"Current text (L{show_s}-L{show_e}, exact):\n{_numbered(file_lines, show_s, show_e)}{truncated}\n"
        "Copy `old_string` from these lines (without the line-number prefix) and retry."
    )
    return hint, (show_s, show_e)


def hint_for_tool_failure(tool_name: str, tool_input: dict[str, Any], error: str) -> tuple[str, str, tuple[int, int] | None] | None:
    """Dispatch for Claude Code `Edit` / `MultiEdit` failures. Returns (file_path, hint, range)."""
    fp = str(tool_input.get("file_path") or "").strip()
    if not fp:
        return None
    if tool_name == "Edit":
        res = edit_failure_hint(
            fp, str(tool_input.get("old_string") or ""), error,
            replace_all=bool(tool_input.get("replace_all")),
        )
        return (fp, res[0], res[1]) if res else None
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
        try:
            text = Path(fp).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
        except OSError:
            return None
        cur = text
        for idx, ed in enumerate(edits):
            if not isinstance(ed, dict):
                return None
            old = str(ed.get("old_string") or "").replace("\r\n", "\n")
            new = str(ed.get("new_string") or "").replace("\r\n", "\n")
            ra = bool(ed.get("replace_all"))
            cnt = cur.count(old) if old else 0
            if cnt == 1 or (cnt > 1 and ra):
                cur = cur.replace(old, new) if ra else cur.replace(old, new, 1)
                continue
            # First failing edit. Line numbers refer to the file on disk, so only
            # diagnose against it when no earlier edit changed the text.
            if cur != text and old not in text:
                return fp, (
                    f"[Terser] MultiEdit edit #{idx + 1} `old_string` does not match the text produced by the "
                    "earlier edits in the same call. Check it against those edits' `new_string` values."
                ), None
            res = edit_failure_hint(fp, old, error, replace_all=ra, edit_index=idx)
            return (fp, res[0], res[1]) if res else None
    return None
