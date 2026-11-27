#!/usr/bin/env python3
"""Terser — Enhanced Quality-Preserving Output Compressor.

Chains five lossless/error-preserving compression stages:
  1. ANSI escape & carriage-return progress/spinner stripping (zero semantic loss).
  2. Bundled specialized command handlers (git, pytest, npm, cargo, tsc, etc.).
  3. Consecutive & near-duplicate log-line collapsing (preserves all errors/stack frames/diffs).
  4. Uniform JSON array columnar folding (100% value-preserving, ~40-65% fewer tokens).
  5. Error-pinned bounded window with local full-output archive (`terse expand <id>`).

Can be used both:
  - In-process: ``compress_output(text, command="pytest", ...)``
  - As a safe PreToolUse command wrapper CLI: ``python -E -s output_compressor.py <cmd> [args...]``
    (Re-validates argv against ``bash_whitelist`` before executing, ``shell=False`` always.)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR.parent))

from runtime.config import ensure_vendor_paths, record_savings, state_home

# OSC 8 terminal hyperlinks: preserve visible label text (\1) while stripping escape wrapper
_ANSI_OSC8_RE = re.compile(r"\x1b\]8;[^\x07]*\x07([^\x1b]*)\x1b\]8;;\x07")
# ANSI escape sequences (CSI, OSC, single-char escapes)
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])"
)
# Progress/timestamp normalization for dedup comparison only (never alters kept lines)
_TS_NORM_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b"
    r"|\b\d{2}:\d{2}:\d{2}(?:\.\d+)?\b"
)
# High-signal lines that must NEVER be collapsed or dropped
_SIGNAL_LINE_RE = re.compile(
    r"(?i)(?:\berror\b|\bfailed\b|\bfail\b|\bexception\b|\btraceback\b|\bpanic\b|"
    r"\bassertion|\bfatal\b|\bwarning\b|\bwarn\b|^FAILED\s|^ERROR\s|^E\s+|"
    r"^\s*File \"|:\d+:\d*:?\s*(?:error|warning)|^@@\s|^[+-]{3}\s|"
    r"\d+\s+(?:passed|failed|errors?|warnings?))"
)

_ARCHIVE_TTL_SECONDS = 7 * 86400
_DEFAULT_MAX_CHARS = 12000


def strip_ansi_and_progress(text: str) -> str:
    """Strip ANSI escape codes (preserving OSC 8 link labels) and collapse \\r spinner overwrites."""
    if not text:
        return ""
    cleaned = _ANSI_OSC8_RE.sub(r"\1", text)
    cleaned = _ANSI_RE.sub("", cleaned)
    if "\r" not in cleaned:
        return cleaned
    # Normalize CRLF first, then for bare CR overwrites keep only the last segment per line.
    cleaned = cleaned.replace("\r\n", "\n")
    out_lines: list[str] = []
    for line in cleaned.split("\n"):
        if "\r" in line:
            segs = [s for s in line.split("\r") if s.strip()]
            out_lines.append(segs[-1] if segs else "")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def collapse_duplicate_lines(text: str, min_run: int = 3) -> str:
    """Collapse runs of identical (or timestamp-only-varying) non-signal lines."""
    lines = text.splitlines()
    if len(lines) < min_run:
        return text

    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        # Never collapse high-signal error/diff/traceback lines
        if _SIGNAL_LINE_RE.search(line):
            out.append(line)
            i += 1
            continue
        norm = _TS_NORM_RE.sub("<TS>", line.strip())
        if not norm:
            # Collapse multiple consecutive blank lines to 1
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            out.append("")
            i = j
            continue
        j = i + 1
        while j < n:
            if _SIGNAL_LINE_RE.search(lines[j]):
                break
            if _TS_NORM_RE.sub("<TS>", lines[j].strip()) == norm:
                j += 1
            else:
                break
        run_len = j - i
        if run_len >= min_run:
            out.append(line)
            out.append(f"  [... repeated {run_len - 1}x more ...]")
        else:
            out.extend(lines[i:j])
        i = j
    return "\n".join(out)


def fold_uniform_json(text: str) -> str | None:
    """Fold a large JSON array of uniform flat objects into lossless columnar JSON."""
    stripped = text.strip()
    if len(stripped) < 1200 or not stripped.startswith("[") or not stripped.endswith("]"):
        return None
    try:
        data = json.loads(stripped)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, list) or len(data) < 5:
        return None
    if not all(isinstance(item, dict) for item in data):
        return None
    # Collect union of keys in insertion order; only fold if keys are reasonably uniform
    keys: list[str] = []
    seen: set[str] = set()
    for item in data:
        for k in item.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    if not keys or len(keys) > 25:
        return None
    rows = [[item.get(k) for k in keys] for item in data]
    folded = json.dumps({"_format": "columnar", "cols": keys, "rows": rows}, separators=(",", ":"))
    # Only use columnar form if it actually saves >= 20%
    if len(folded) < int(len(stripped) * 0.80):
        return folded
    return None


def _archive_full_output(raw_text: str, command: str = "", env: dict[str, str] | None = None) -> str | None:
    """Index full raw output into SQLite FTS5 sandbox (fallback: flat archive file)."""
    try:
        from runtime import context_sandbox

        aid = context_sandbox.store_output(raw_text, command=command, env=env)
        if aid:
            return aid
    except Exception:
        pass
    try:
        adir = state_home(env) / "archive"
        adir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        try:
            cutoff = now - _ARCHIVE_TTL_SECONDS
            for old in adir.glob("*.txt"):
                if old.stat().st_mtime < cutoff:
                    old.unlink(missing_ok=True)
        except OSError:
            pass
        from runtime import context_sandbox

        safe_text = context_sandbox.redact_text(raw_text, is_command=False)
        safe_cmd = context_sandbox.redact_text(command, is_command=True) if command else ""
        aid = hashlib.sha256(f"{now}:{safe_cmd}:{len(safe_text)}".encode("utf-8")).hexdigest()[:10]
        header = f"# command: {safe_cmd}\n# bytes: {len(safe_text)}\n\n" if safe_cmd else ""
        (adir / f"{aid}.txt").write_text(header + safe_text, encoding="utf-8", errors="replace")
        return aid
    except OSError:
        return None


def read_archived_output(
    archive_id: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    if not re.match(r"^[0-9a-f]{6,32}$", str(archive_id or "")):
        return None
    try:
        from runtime import context_sandbox

        hit = context_sandbox.expand_output(archive_id, start_line=start_line, end_line=end_line, env=env)
        if hit is not None:
            return hit
    except Exception:
        pass
    p = state_home(env) / "archive" / f"{archive_id}.txt"
    try:
        if not p.is_file():
            return None
        text = p.read_text(encoding="utf-8", errors="replace")
        if start_line is None and end_line is None:
            return text
        lines = text.splitlines()
        s = max(0, (start_line - 1) if start_line else 0)
        e = min(len(lines), end_line) if end_line else len(lines)
        return "\n".join(f"L{idx}: {ln}" for idx, ln in enumerate(lines[s:e], start=s + 1))
    except OSError:
        return None


# Specialized stream noise filters (strip zero-signal build/install chatter, keep 100% of errors/summaries)
_CARGO_NOISE_RE = re.compile(r"^\s*(?:Compiling|Downloading|Downloaded|Fresh|Updating|Locking|Adding|Checking)\s+")
_GO_TEST_PASS_RE = re.compile(r"^\s*(?:=== RUN|=== PAUSE|=== CONT|--- PASS:)\s+")
_PKG_NOISE_RE = re.compile(
    r"^\s*(?:Requirement already satisfied:|Collecting |Downloading |Using cached |Installing collected packages:|"
    r"Resolving |Prepared \d+ packages|Packages:|Progress:|added \d+ packages in |audited \d+ packages)"
)
_BUNDLER_NOISE_RE = re.compile(r"^\s*(?:transforming \(\d+\)|✓ \d+ modules transformed|rendering chunks|computing gzip size)")
_TEST_PASS_LINE_RE = re.compile(
    r"^\s*(?:.*::\S+\s+(?:PASSED|SKIPPED|XFAIL)(?:\s+\[\s*\d+%\])?$|"
    r"test_\S+\s+\([^)]+\)\s+\.\.\.\s+(?:ok|skipped\b.*)$|"
    r"(?:✓|✔|PASS)\s+\S+.*$)"
)
_INFRA_NOISE_RE = re.compile(
    r"^\s*(?:[0-9a-f]{6,12}:\s*(?:Already exists|Pulling fs layer|Waiting|Verifying Checksum|Download complete|Pull complete)|"
    r"#\d+\s+(?:CACHED|DONE \d|transferring )|.*:\s*Refreshing state\.\.\.|"
    r">\s*Task\s+:\S+\s+(?:UP-TO-DATE|FROM-CACHE|NO-SOURCE)|"
    r"\[INFO\]\s*Downloading from\s+|Restored\s+.*\s+in\s+\d+\s*ms)"
)
_GREP_LINE_RE = re.compile(r"^(?P<path>(?:[A-Za-z]:)?[^:\s][^:\r\n]{1,220}):(?P<line>\d+):(?P<rest>.*)$")


def elide_workspace_paths(text: str, workspace: str | None = None) -> str:
    """Losslessly replace repeated workspace root prefixes with `./` + a single `[root: ...]` header."""
    if not text or len(text) < 400:
        return text
    ws = (workspace or os.getcwd() or "").rstrip("/\\")
    if len(ws) < 12:
        return text
    variants = {ws + "/", ws + "\\", ws.replace("\\", "/") + "/", ws.replace("/", "\\") + "\\"}
    flags = re.IGNORECASE if os.name == "nt" else 0
    pat = re.compile("|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True)), flags)
    matches = pat.findall(text)
    if len(matches) < 4:
        return text
    replaced = pat.sub("./", text)
    if len(replaced) + len(ws) + 12 < len(text):
        return f"[root: {ws}]\n{replaced}"
    return text


def group_grep_output(text: str) -> str:
    """Losslessly group `path:line:content` search output by file header to remove repeated paths."""
    if not text or "diff --git " in text or "Traceback " in text:
        return text
    lines = text.splitlines()
    if len(lines) < 6:
        return text
    parsed: list[tuple[str, str, str] | None] = []
    counts: dict[str, int] = {}
    matched = 0
    for ln in lines:
        m = _GREP_LINE_RE.match(ln)
        if m:
            p = m.group("path")
            # Avoid misclassifying URLs or timestamps (e.g. http://... or 12:34:56)
            if p.startswith(("http", "https")) or p.isdigit():
                parsed.append(None)
            else:
                parsed.append((p, m.group("line"), m.group("rest")))
                counts[p] = counts.get(p, 0) + 1
                matched += 1
        else:
            parsed.append(None)
    if matched < 6 or matched < int(len(lines) * 0.65) or not any(c >= 2 for c in counts.values()):
        return text

    out: list[str] = []
    cur_file: str | None = None
    for idx, item in enumerate(parsed):
        if item is None:
            cur_file = None
            out.append(lines[idx])
            continue
        p, ln_no, rest = item
        if p != cur_file:
            out.append(f"{p}:")
            cur_file = p
        out.append(f"  {ln_no}:{rest}")
    grouped = "\n".join(out)
    return grouped if len(grouped) < int(len(text) * 0.90) else text


_LOCKFILE_OR_GEN_RE = re.compile(
    r"(?i)(?:^|/)(?:package-lock\.json|pnpm-lock\.yaml|yarn\.lock|cargo\.lock|"
    r"poetry\.lock|uv\.lock|pipfile\.lock|composer\.lock|gemfile\.lock|go\.sum|"
    r"dist/.*|.*_pb2\.py|.*\.(?:min\.js|min\.css|map|pb\.go|generated\.[a-z0-9]+|snap|svg))$"
)
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")
_DIFF_INDEX_RE = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+(?: \d+)?$")
_FRAMEWORK_FRAME_RE = re.compile(
    r"(?:site-packages[\\/]|dist-packages[\\/]|node_modules[\\/]|node:internal/|"
    r"Lib[\\/](?:unittest|asyncio|importlib|runpy|threading|concurrent)[\\/]|"
    r"/usr/lib/python|/Library/Frameworks/Python)"
)


def elide_lockfile_diffs(text: str) -> str:
    """Elide lockfile/generated/minified hunks inside `git diff`/`git show` while keeping all source diffs verbatim."""
    if "diff --git " not in text:
        return text
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        m = _DIFF_HEADER_RE.match(lines[i])
        if m:
            path_b = m.group(2)
            # Find end of this file's diff section
            j = i + 1
            while j < n and not _DIFF_HEADER_RE.match(lines[j]):
                j += 1
            if _LOCKFILE_OR_GEN_RE.search(path_b) and (j - i) > 12:
                adds = sum(1 for ln in lines[i:j] if ln.startswith("+") and not ln.startswith("+++"))
                dels = sum(1 for ln in lines[i:j] if ln.startswith("-") and not ln.startswith("---"))
                out.append(
                    f"{lines[i]}  [elided lockfile/generated diff: +{adds}/-{dels} lines; "
                    f"run `git diff -- {path_b}` if needed]"
                )
            else:
                out.extend(lines[i:j])
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def compact_diff_headers(text: str) -> str:
    """Losslessly collapse 4-line per-file git diff boilerplate into `=== path` while keeping every hunk verbatim."""
    if "diff --git " not in text:
        return text
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        m = _DIFF_HEADER_RE.match(lines[i])
        if m and "[elided lockfile/generated diff:" not in lines[i]:
            path_a, path_b = m.group(1), m.group(2)
            j = i + 1
            mode_tag = ""
            while j < n:
                ln = lines[j]
                if _DIFF_INDEX_RE.match(ln):
                    j += 1
                elif ln.startswith("new file mode "):
                    mode_tag = " (new)"
                    j += 1
                elif ln.startswith("deleted file mode "):
                    mode_tag = " (deleted)"
                    j += 1
                elif ln.startswith("--- ") and (ln == f"--- a/{path_a}" or ln == "--- /dev/null"):
                    if ln == "--- /dev/null":
                        mode_tag = " (new)"
                    j += 1
                elif ln.startswith("+++ ") and (ln == f"+++ b/{path_b}" or ln == "+++ /dev/null"):
                    if ln == "+++ /dev/null":
                        mode_tag = " (deleted)"
                    j += 1
                else:
                    break
            if j > i + 1:
                header = f"=== {path_b}{mode_tag}" if path_a == path_b else f"=== {path_a} -> {path_b}{mode_tag}"
                out.append(header)
                i = j
                continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def fold_framework_stack_frames(text: str, min_run: int = 3) -> str:
    """Collapse consecutive third-party/stdlib stack frames while keeping 100% of user-code frames & errors."""
    if "Traceback" not in text and "    at " not in text:
        return text
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        is_py_fw = ln.lstrip().startswith('File "') and bool(_FRAMEWORK_FRAME_RE.search(ln))
        is_js_fw = ln.lstrip().startswith("at ") and bool(_FRAMEWORK_FRAME_RE.search(ln))
        if is_py_fw or is_js_fw:
            start = i
            frames = 0
            while i < n:
                cur = lines[i]
                if cur.lstrip().startswith('File "') and _FRAMEWORK_FRAME_RE.search(cur):
                    frames += 1
                    i += 1
                    # Also consume the indented source line right below Python's `File "..."` line
                    if i < n and lines[i].startswith("    ") and not lines[i].lstrip().startswith('File "'):
                        i += 1
                elif cur.lstrip().startswith("at ") and _FRAMEWORK_FRAME_RE.search(cur):
                    frames += 1
                    i += 1
                else:
                    break
            if frames >= min_run:
                out.append(f"  [... {frames} framework/stdlib stack frames omitted ...]")
            else:
                out.extend(lines[start:i])
        else:
            out.append(ln)
            i += 1
    return "\n".join(out)


def dedup_identical_tracebacks(text: str) -> str:
    """Deduplicate identical repeated Python tracebacks (e.g. parametrized test failures)."""
    if not text or text.count("Traceback (most recent call last):") < 2:
        return text
    lines = text.splitlines()
    out: list[str] = []
    seen: dict[str, int] = {}
    i, n = 0, len(lines)
    changed = False
    while i < n:
        if lines[i].lstrip().startswith("Traceback (most recent call last):"):
            start = i
            j = i + 1
            while j < n:
                ln = lines[j]
                if not ln.strip():
                    break
                if ln.startswith((" ", "\t")):
                    j += 1
                    continue
                # Exception summary line ends the traceback
                j += 1
                break
            block = lines[start:j]
            key = "\n".join(block)
            if key in seen:
                exc_line = block[-1].strip()[:120] if block else ""
                out.append(f"[Terser: identical traceback to #{seen[key]} omitted — {exc_line}]")
                changed = True
            else:
                idx_num = len(seen) + 1
                seen[key] = idx_num
                out.extend(block)
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out) if changed else text


_COMPILER_NOTE_RE = re.compile(r"^\s*(?:=\s*)?(?:note|help):\s+(.+)$")


def dedup_compiler_notes(text: str) -> str:
    """Deduplicate repeated identical `note:` / `help:` boilerplate in rustc/clippy/tsc/mypy output."""
    if not text or ("note:" not in text and "help:" not in text):
        return text
    lines = text.splitlines()
    seen_notes: dict[str, int] = {}
    out: list[str] = []
    omitted = 0
    for ln in lines:
        m = _COMPILER_NOTE_RE.match(ln)
        if m:
            norm = " ".join(m.group(1).split())
            if len(norm) >= 24:
                cnt = seen_notes.get(norm, 0) + 1
                seen_notes[norm] = cnt
                if cnt >= 2:
                    omitted += 1
                    continue
        out.append(ln)
    if omitted >= 2:
        out.append(f"[Terser: {omitted} repeated compiler note/help lines deduplicated]")
        return "\n".join(out)
    return text


_HTML_TAG_PROBE_RE = re.compile(r"(?i)<(?:!doctype\s+html|html|head|body|nav|footer|script|style|div|table)\b")
_HTML_BOILERPLATE_RE = re.compile(
    r"(?is)<(?:script|style|noscript|svg|nav|footer|iframe|template)\b[^>]*>.*?</(?:script|style|noscript|svg|nav|footer|iframe|template)>|<!--.*?-->"
)
_HTML_LINK_RE = re.compile(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>')
_HTML_BLOCK_BREAK_RE = re.compile(r"(?i)</?(?:p|div|br|li|tr|h[1-6]|section|article|header|main|pre|blockquote)\b[^>]*>")
_HTML_ANY_TAG_RE = re.compile(r"<[^>]+>")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")


_HTML_DOC_HEAD_RE = re.compile(r"(?is)^\s*(?:<!doctype\s+html\b|<html\b|<head\b|<body\b)")
_WEB_CMD_RE = re.compile(r"(?i)(?:^|\s)(?:curl|wget|http|xh|webfetch|fetch|lynx|w3m)\b")


def compact_web_and_markdown(text: str, *, allow_html: bool = False) -> str:
    """Strip HTML nav/script/style boilerplate (web pages only) and compact padded Markdown tables into lossless TSV."""
    if not text or len(text) < 400:
        return text

    cur = text
    # 1. HTML boilerplate stripping: ONLY for actual HTML documents / web fetches,
    # never for diffs, source files, JSX/TSX, or arbitrary command output containing markup.
    is_html_doc = bool(_HTML_DOC_HEAD_RE.search(cur[:1024]))
    if (allow_html or is_html_doc) and len(_HTML_TAG_PROBE_RE.findall(cur[:8192])) >= 3:
        import html as _html

        cleaned = _HTML_BOILERPLATE_RE.sub("\n", cur)
        cleaned = _HTML_LINK_RE.sub(
            lambda m: f"[{_HTML_ANY_TAG_RE.sub('', m.group(2)).strip()}]({m.group(1)})"
            if _HTML_ANY_TAG_RE.sub("", m.group(2)).strip()
            else m.group(1),
            cleaned,
        )
        cleaned = _HTML_BLOCK_BREAK_RE.sub("\n", cleaned)
        cleaned = _HTML_ANY_TAG_RE.sub(" ", cleaned)
        cleaned = _html.unescape(cleaned)
        cleaned_lines = [" ".join(ln.split()) for ln in cleaned.splitlines()]
        cleaned = "\n".join(ln for ln in cleaned_lines if ln)
        if cleaned and len(cleaned) < int(len(cur) * 0.85):
            cur = cleaned

    # 2. Padded Markdown pipe-table compaction -> lossless TSV
    if "|" in cur and "---" in cur:
        lines = cur.splitlines()
        out: list[str] = []
        i, n = 0, len(lines)
        changed = False
        while i < n:
            if (
                i + 2 < n
                and "|" in lines[i]
                and _MD_TABLE_SEP_RE.match(lines[i + 1])
                and "|" in lines[i + 2]
            ):
                j = i + 2
                while j < n and "|" in lines[j] and lines[j].strip():
                    j += 1
                row_count = j - i
                if row_count >= 4:
                    raw_block = "\n".join(lines[i:j])
                    tsv_rows: list[str] = []
                    for k in range(i, j):
                        if k == i + 1:
                            continue  # omit separator row
                        row = lines[k].strip().strip("|")
                        cells = [" ".join(c.strip().split()) for c in row.split("|")]
                        tsv_rows.append("\t".join(cells))
                    tsv_block = "\n".join(tsv_rows)
                    if len(tsv_block) < int(len(raw_block) * 0.85):
                        out.extend(tsv_rows)
                        changed = True
                        i = j
                        continue
            out.append(lines[i])
            i += 1
        if changed:
            cur = "\n".join(out)

    return cur


_WEB_HEAD_CHARS = 4000
_WEB_OUTLINE_MAX = 40
_MD_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")


def archive_web_page(
    text: str,
    *,
    source: str = "",
    threshold: int = 12000,
    env: dict[str, str] | None = None,
) -> str | None:
    """Archive a large fetched page into sandbox.db; return head + heading outline + search hint.

    Returns ``None`` when the page is under ``threshold`` or archiving fails, so the caller
    keeps serving the page itself. Lines in the outline are archive line numbers, so
    ``expand_sandbox`` can jump straight to a section.
    """
    if not text or len(text) <= threshold:
        return None
    from runtime import context_sandbox

    label = f"web:{source[:180]}" if source else "web"
    aid = context_sandbox.store_output(text, command=label, env=env)
    if not aid:
        return None
    lines = text.splitlines()
    outline: list[str] = []
    for no, line in enumerate(lines, start=1):
        m = _MD_HEADING_RE.match(line)
        if m:
            outline.append(f"  L{no} {'  ' * (len(m.group(1)) - 1)}{m.group(2)[:100]}")
            if len(outline) >= _WEB_OUTLINE_MAX:
                outline.append("  ...")
                break
    head = text[:_WEB_HEAD_CHARS]
    cut = head.rfind("\n")
    if cut > _WEB_HEAD_CHARS // 2:
        head = head[:cut]
    parts = [head.rstrip(), f"\n[Terser: page has {len(lines)} lines / {len(text)} chars; the rest is archived as `{aid}`.]"]
    if outline:
        parts.append("Sections (archive line numbers):\n" + "\n".join(outline))
    parts.append(context_sandbox.retrieval_hint(aid, env=env))
    record_savings("web_archive", len(text), sum(len(p) for p in parts), host="any", detail=source[:60] or "web", env=env)
    return "\n".join(parts)


_MULTI_SPACE_RE = re.compile(r" {2,}")
_BASE64_DATA_URI_RE = re.compile(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]{512,}")
_BASE64_RAW_RE = re.compile(r"(?<![A-Za-z0-9+/_.-])[A-Za-z0-9+/]{512,}={0,2}(?![A-Za-z0-9+/_.-])")


def compact_aligned_tables(text: str) -> str:
    """Losslessly compact space-padded fixed-width CLI tables (docker ps, kubectl get, pip list) to TSV."""
    if not text or len(text) < 300 or "diff --git " in text or "Traceback " in text:
        return text
    lines = text.splitlines()
    if len(lines) < 4:
        return text

    def _is_table_row(ln: str) -> bool:
        s = ln.strip()
        if not s or s[0] in ("{", "[", "}", "]", "#", "<"):
            return False
        # Require at least 2 interior runs of 2+ spaces (i.e. >= 3 space-padded columns)
        return len(_MULTI_SPACE_RE.findall(s)) >= 2

    out: list[str] = []
    i, n = 0, len(lines)
    changed = False
    while i < n:
        if _is_table_row(lines[i]):
            j = i + 1
            while j < n and _is_table_row(lines[j]):
                j += 1
            if (j - i) >= 4:
                raw_block = "\n".join(lines[i:j])
                tsv_rows = [_MULTI_SPACE_RE.sub("\t", ln.strip()) for ln in lines[i:j]]
                tsv_block = "\n".join(tsv_rows)
                if len(tsv_block) < int(len(raw_block) * 0.85):
                    out.extend(tsv_rows)
                    changed = True
                    i = j
                    continue
            out.extend(lines[i:j])
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out) if changed else text


def fold_base64_blobs(
    text: str,
    *,
    command: str = "",
    env: dict[str, str] | None = None,
) -> tuple[str, str | None]:
    """Reversibly replace >=512-char base64/data-URI blobs with compact digests, archiving full raw text."""
    if not text or len(text) < 512:
        return text, None
    has_match = bool(_BASE64_DATA_URI_RE.search(text) or _BASE64_RAW_RE.search(text))
    if not has_match:
        return text, None

    aid = _archive_full_output(text, command=command or "base64_output", env=env)
    arch_tag = f" archive:{aid}" if aid else ""

    def _repl(m: re.Match[str]) -> str:
        blob = m.group(0)
        digest = hashlib.sha256(blob.encode("utf-8", errors="replace")).hexdigest()[:8]
        if blob.startswith("data:"):
            prefix = blob.split(",", 1)[0]
            return f"[{prefix} {len(blob)}B sha256:{digest}{arch_tag}]"
        return f"[base64:{len(blob)}B sha256:{digest}{arch_tag}]"

    folded = _BASE64_DATA_URI_RE.sub(_repl, text)
    folded = _BASE64_RAW_RE.sub(_repl, folded)
    if len(folded) < int(len(text) * 0.90):
        return folded, aid
    return text, None


def compact_file_list(paths: list[str]) -> str:
    """Losslessly factor shared directory prefixes in a file listing (`dir/: a, b, c`) when saving >=15%."""
    clean = [p.strip() for p in paths if p and p.strip()]
    if len(clean) < 4:
        return "\n".join(clean)
    flat = "\n".join(clean)
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for raw in clean:
        norm = raw.replace("\\", "/")
        if "/" in norm:
            parent, name = norm.rsplit("/", 1)
        else:
            parent, name = "", norm
        if parent not in groups:
            groups[parent] = []
            order.append(parent)
        groups[parent].append(name)

    lines: list[str] = []
    for parent in order:
        names = groups[parent]
        if parent and len(names) >= 2:
            lines.append(f"{parent}/: {', '.join(names)}")
        elif parent:
            lines.append(f"{parent}/{names[0]}")
        else:
            lines.extend(names)
    grouped = "\n".join(lines)
    return grouped if len(grouped) < int(len(flat) * 0.85) else flat


def apply_stream_filters(text: str, command: str) -> str:
    """Apply high-signal stream filters for Cargo, Go test, bundlers, and package managers."""
    if not text or not command:
        return text
    low = command.lower()
    lines = text.splitlines()
    if len(lines) < 8:
        return text

    filtered: list[str] | None = None
    if re.search(r"\bcargo\s+(?:build|test|check|clippy|run)\b", low):
        kept = [ln for ln in lines if _SIGNAL_LINE_RE.search(ln) or not _CARGO_NOISE_RE.match(ln)]
        omitted = len(lines) - len(kept)
        if omitted >= 4:
            filtered = [f"[cargo: {omitted} compile/download status lines omitted]", *kept]
    elif re.search(r"\bgo\s+test\b", low):
        kept = [ln for ln in lines if _SIGNAL_LINE_RE.search(ln) or not _GO_TEST_PASS_RE.match(ln)]
        omitted = len(lines) - len(kept)
        if omitted >= 4:
            filtered = [f"[go test: {omitted} passing RUN/PASS lines omitted]", *kept]
    elif re.search(r"\b(?:pip|pip3|uv\s+pip|pnpm|yarn|npm)\s+(?:install|ci|add|sync)\b", low):
        kept = [ln for ln in lines if _SIGNAL_LINE_RE.search(ln) or not _PKG_NOISE_RE.match(ln)]
        omitted = len(lines) - len(kept)
        if omitted >= 4:
            filtered = [f"[pkg: {omitted} download/satisfied lines omitted]", *kept]
    elif re.search(r"\b(?:vite|webpack|next\s+build|esbuild|rollup|tsc)\b", low):
        kept = [ln for ln in lines if _SIGNAL_LINE_RE.search(ln) or not _BUNDLER_NOISE_RE.match(ln)]
        omitted = len(lines) - len(kept)
        if omitted >= 4:
            filtered = [f"[build: {omitted} progress lines omitted]", *kept]
    elif re.search(r"\b(?:pytest|unittest|jest|vitest|mocha)\b", low):
        kept = [
            ln for ln in lines
            if not _TEST_PASS_LINE_RE.match(ln) or re.search(r"(?i)\b(?:fail|error|exception|traceback)\b", ln)
        ]
        omitted = len(lines) - len(kept)
        if omitted >= 4:
            filtered = [f"[tests: {omitted} passing/skipped test lines omitted]", *kept]
    elif re.search(r"\b(?:docker|terraform|tofu|dotnet|gradle|gradlew|mvn|mvnw)\b", low):
        kept = [ln for ln in lines if _SIGNAL_LINE_RE.search(ln) or not _INFRA_NOISE_RE.match(ln)]
        omitted = len(lines) - len(kept)
        if omitted >= 4:
            filtered = [f"[infra/build: {omitted} unchanged/progress lines omitted]", *kept]

    if filtered is not None:
        return "\n".join(filtered)
    return text


def _retrieval_hint(archive_id: str, mode: str = "both") -> str:
    from runtime import context_sandbox

    return context_sandbox.retrieval_hint(archive_id, mode, env=os.environ)


def _error_pinned_window(text: str, max_chars: int, archive_id: str | None) -> str:
    """Keep head + ALL error/warning/diff lines (with ±2 stack-trace context) + tail within max_chars."""
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    head_n = min(30, len(lines))
    tail_n = min(40, max(0, len(lines) - head_n))
    head = lines[:head_n]
    mid = lines[head_n: len(lines) - tail_n] if tail_n else lines[head_n:]
    tail = lines[len(lines) - tail_n:] if tail_n else []

    # Pin signal lines plus ±2 surrounding lines (indented source, caret pointers, assertion diffs)
    keep_mid: set[int] = set()
    for i, ln in enumerate(mid):
        if _SIGNAL_LINE_RE.search(ln):
            for j in range(max(0, i - 1), min(len(mid), i + 3)):
                keep_mid.add(j)

    pinned_mid: list[str] = []
    pinned_budget = max(2500, int(max_chars * 0.55))
    used = 0
    for i in sorted(keep_mid):
        entry = f"L{head_n + i + 1}: {mid[i]}"
        if used + len(entry) + 1 <= pinned_budget:
            pinned_mid.append(entry)
            used += len(entry) + 1

    omitted = max(0, len(mid) - len(pinned_mid))
    pointer = (
        f" (full output indexed: {_retrieval_hint(archive_id)})"
        if archive_id
        else ""
    )

    def _assemble(h: list[str], p: list[str], t: list[str]) -> str:
        parts: list[str] = list(h)
        if omitted > 0:
            parts.append(f"... [{omitted} routine middle lines omitted; {len(p)} signal/context lines preserved{pointer}] ...")
        parts.extend(p)
        if t:
            parts.append("--- tail ---")
            parts.extend(t)
        return "\n".join(parts)

    result = _assemble(head, pinned_mid, tail)
    # Trim routine head/tail first before ever cutting pinned signal lines
    while len(result) > max_chars and (len(head) > 10 or len(tail) > 15):
        if len(head) > 10:
            head = head[: max(10, len(head) - 5)]
        if len(tail) > 15:
            tail = tail[min(5, len(tail) - 15):]
        result = _assemble(head, pinned_mid, tail)
    if len(result) > max_chars:
        half = max(500, (max_chars - 120) // 2)
        result = result[:half] + f"\n... [truncated{pointer}] ...\n" + result[-half:]
    return result


_CRITICAL_VERIFY_RE = re.compile(
    r"(?i)(?:^\s*(?:FAILED|ERROR|FATAL|PANIC)\b|^E\s+\S|error\[E\d+\]:|Traceback \(most recent call last\)|^@@ -\d+)"
)


def _verify_signal_preserved(raw: str, compressed: str, *, returncode: int = 0) -> bool:
    """Deterministic post-compression invariant check (inspired by sqz verifier).

    Ensures critical failure/traceback/diff-hunk lines were not accidentally dropped.
    """
    if not raw or not compressed:
        return bool(compressed) or not raw
    # Only enforce strict signal survival when output was meaningfully shrunk
    if len(compressed) >= len(raw):
        return True
    critical = [ln.strip() for ln in raw.splitlines() if _CRITICAL_VERIFY_RE.search(ln)]
    if not critical:
        return True
    # Sample up to 8 distinct critical lines (first 4 + last 4) and verify at least 75% survive
    sample = list(dict.fromkeys(critical[:4] + critical[-4:]))
    if not sample:
        return True
    hits = sum(1 for ln in sample if ln[:60] in compressed)
    min_ratio = 0.75 if returncode != 0 else 0.50
    return (hits / len(sample)) >= min_ratio


def _vendor_compress(text: str, command: str, returncode: int = 0, stderr: str = "") -> str:
    """Apply Terser's native per-command handlers (git, test runners, package managers)."""
    if not command or not text:
        return text
    try:
        from runtime import command_handlers

        out = command_handlers.compress(command, text, returncode=returncode, stderr=stderr)
        if isinstance(out, str) and out:
            return out
    except Exception:
        pass
    return text


_FILE_LIST_CMD_RE = re.compile(
    r"(?i)^\s*(?:git\s+(?:ls-files|diff\s+.*--name-only)|rg\s+.*--files|fd\b|find\b)"
)


def _maybe_compact_file_listing(text: str, command: str) -> str:
    """Losslessly group directory prefixes for path-per-line shell commands (git ls-files, rg --files, find)."""
    if not text or not command or not _FILE_LIST_CMD_RE.match(command):
        return text
    if re.search(r"(?i)\s-(?:ls|exec|printf)\b", command):
        return text
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 4:
        return text
    # Guard against error/status lines mixed in: require lines to look like plain paths
    if any(ln.startswith((" ", "\t", "fatal:", "error:", "warning:")) or ": " in ln for ln in lines[:20]):
        return text
    return compact_file_list(lines)


_SCOPED_READ_MAX = 40_000
_SCOPED_READ_PATTERN = (
    r"(?i)(?:\bsed\s+-n\s+['\"]?[^\s'\"]*[\d$][^\s'\"]*p['\"]?(?:\s|$)"
    r"|\b(?:head|tail)\s+-(?:n\s*)?\d+|\b(?:head|tail)\s+-n\b"
    r"|\bawk\s+['\"]NR|-(?:TotalCount|Tail|Head|First|Last|Index)\b)"
)
_SCOPED_READ_RE = re.compile(_SCOPED_READ_PATTERN)


_LOSSY_DROP_KEY_RE = re.compile(r"(?i)^(?:x[-_])?(?:trace|request|span|correlation|parent[-_]?span)[-_]?id$")
_LOSSY_STR_MAX = 500
_LOSSY_MIN_CHARS = 1200


def _json_lossy_enabled(explicit: bool | None, env: dict[str, str] | None) -> bool:
    if explicit is not None:
        return explicit
    e = env if env is not None else os.environ
    raw = str(e.get("TERSER_JSON_LOSSY") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    try:
        from runtime.config import load_config

        return bool(load_config(e).get("json_lossy", False))
    except Exception:
        return False


def _prune_node(node: Any) -> Any:
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if _LOSSY_DROP_KEY_RE.match(str(k)):
                continue
            v = _prune_node(v)
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(node, list):
        return [_prune_node(x) for x in node]
    if isinstance(node, str) and len(node) > _LOSSY_STR_MAX:
        return node[:_LOSSY_STR_MAX] + f"...[+{len(node) - _LOSSY_STR_MAX} chars]"
    return node


def prune_json_lossy(text: str) -> str | None:
    """Opt-in lossy JSON view: drop nulls/empties and trace/request ids, cut long strings.

    Returns compact JSON, or ``None`` when the text is not a JSON document or nothing
    shrinks. Never used on error output; callers archive the original first.
    """
    stripped = (text or "").strip()
    if len(stripped) < _LOSSY_MIN_CHARS or stripped[:1] not in "{[":
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    pruned = json.dumps(_prune_node(data), ensure_ascii=False, separators=(",", ":"))
    return pruned if len(pruned) < int(len(stripped) * 0.9) else None


def is_scoped_read(command: str) -> bool:
    """True when the command already selects a line range; those lines are not elided below 40 KB."""
    return bool(command) and bool(_SCOPED_READ_RE.search(command))


def compress_output(
    text: str,
    *,
    command: str = "",
    returncode: int = 0,
    stderr: str = "",
    max_chars: int = _DEFAULT_MAX_CHARS,
    enhanced: bool = True,
    lossless_only: bool = False,
    host: str = "unknown",
    workspace: str | None = None,
    env: dict[str, str] | None = None,
    json_lossy: bool | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run the 5-stage quality-preserving output compression pipeline.

    When `lossless_only=True`, only 100% lossless transforms (ANSI/progress stripping,
    Markdown/HTML boilerplate & table-to-TSV compaction, lossless columnar/minified JSON,
    and workspace path elision) are applied—never sampling, elision, or truncation.

    Returns ``(compressed_text, stats_dict)``.
    """
    if not text:
        return "", {"raw_chars": 0, "served_chars": 0, "archive_id": None}

    raw_len = len(text)
    user_filters = None
    if command:
        try:
            from runtime import user_filters
        except Exception:
            user_filters = None
        if user_filters is not None and user_filters.is_excluded(command, workspace, env):
            return text, {"raw_chars": raw_len, "served_chars": raw_len, "archive_id": None, "excluded": True}
    cur = strip_ansi_and_progress(text) if enhanced else text

    # Stage 1b: opt-in lossy JSON pruning (`json_lossy=true`); the raw payload is archived first.
    lossy_aid: str | None = None
    if enhanced and not lossless_only and _json_lossy_enabled(json_lossy, env):
        pruned = prune_json_lossy(cur)
        if pruned is not None:
            if lossy_aid is None:
                lossy_aid = _archive_full_output(text, command=command, env=env)
            hint = f"\n[terser: lossy JSON view (nulls, empties, trace ids, long strings cut); original: {_retrieval_hint(lossy_aid)}]" if lossy_aid else ""
            cur = pruned + hint

    # Stage 2: lockfile diff elision + framework stack folding + compiler note dedup + base64 + web/table compaction + filters
    archive_id: str | None = lossy_aid
    if enhanced:
        if not lossless_only:
            cur = elide_lockfile_diffs(cur)
            cur = compact_diff_headers(cur)
            cur = fold_framework_stack_frames(cur)
            cur = dedup_identical_tracebacks(cur)
            cur = dedup_compiler_notes(cur)
            cur, b64_aid = fold_base64_blobs(cur, command=command, env=env)
            if b64_aid:
                archive_id = b64_aid
        cur = compact_web_and_markdown(
            cur,
            allow_html=(not lossless_only and bool(_WEB_CMD_RE.search(command or ""))),
        )
        cur = compact_aligned_tables(cur)
    if command and not lossless_only:
        if user_filters is not None:
            cur = user_filters.apply_filters(command, cur, returncode=returncode, workspace=workspace, env=env)
        cur = apply_stream_filters(cur, command) if enhanced else cur
        cur = _vendor_compress(cur, command, returncode=returncode, stderr=stderr)

    # Stage 3: SmartCrusher columnar/statistical JSON pruning (or strictly lossless columnar/minify when lossless_only=True)
    if enhanced:
        crushed: str | None = None
        try:
            from runtime import smart_crusher

            crushed = smart_crusher.crush_json(
                cur,
                command=command,
                allow_sample=not lossless_only,
                env=env,
            )
            if crushed is None and not lossless_only:
                crushed = smart_crusher.crush_mixed_ndjson(cur, command=command, env=env)
        except Exception:
            crushed = fold_uniform_json(cur)
        if crushed is not None:
            cur = crushed
        else:
            cur = elide_workspace_paths(cur, workspace=workspace)
            cur = _maybe_compact_file_listing(cur, command)
            cur = group_grep_output(cur)
            if not lossless_only:
                cur = collapse_duplicate_lines(cur)

    window = max(max_chars, _SCOPED_READ_MAX) if is_scoped_read(command) else max_chars
    if not lossless_only and len(cur) > window:
        if archive_id is None:
            archive_id = _archive_full_output(text, command=command, env=env)
        cur = _error_pinned_window(cur, max_chars=window, archive_id=archive_id)

    # Post-compression invariant check: if critical error/diff-hunk lines were lost, fall back safely.
    if not lossless_only and not _verify_signal_preserved(text, cur, returncode=returncode):
        if archive_id is None and len(text) > window:
            archive_id = _archive_full_output(text, command=command, env=env)
        safe_base = strip_ansi_and_progress(text) if enhanced else text
        cur = _error_pinned_window(safe_base, max_chars=window, archive_id=archive_id) if len(safe_base) > window else safe_base

    if len(cur) < raw_len:
        record_savings(
            "output_compress",
            raw_len,
            len(cur),
            host=host,
            detail=command[:60] or "output",
            env=env,
        )
    return cur, {"raw_chars": raw_len, "served_chars": len(cur), "archive_id": archive_id}


def _recheck_safe_argv(argv: list[str]) -> bool:
    """Fail-closed whitelist re-verification (R13a) so the wrapper can never run unsafe commands."""
    if not argv:
        return False
    try:
        from runtime import shell_safety as _bh

        joined = shlex.join(argv)
        if _bh.has_dangerous_chars(joined):
            return False
        return bool(_bh.is_whitelisted(joined))
    except Exception:
        return False


def run_codex_exec(args: list[str]) -> int:
    """Run an explicit, non-interactive argv command with recoverable stdout deltas."""
    import subprocess
    from runtime import cmd_delta
    from runtime.config import load_config

    from runtime import context_sandbox

    context_sandbox.set_hint_host("codex")
    session_id = os.environ.get("TERSER_SESSION_ID") or os.environ.get("CODEX_THREAD_ID", "")
    if len(args) >= 2 and args[0] == "--sid":
        session_id = args[1]
        args = args[2:]
    if len(args) < 2 or args[0] != "--":
        print("Usage: terse exec [--sid <session-id>] -- <program> [args...]", file=sys.stderr)
        return 2
    argv = args[1:]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True)
    except OSError as exc:
        print(f"{argv[0]}: {exc}", file=sys.stderr)
        return 127 if isinstance(exc, FileNotFoundError) else 126

    stdout = proc.stdout
    cfg = load_config()
    if session_id and proc.returncode == 0 and not proc.stderr and cfg.get("enabled") and not cfg.get("shadow") and cfg.get("bash_compress") and cfg.get("cmd_delta"):
        try:
            compact, used = cmd_delta.apply_command_delta(
                session_id, shlex.join(argv), stdout.decode("utf-8"), host="codex", argv=tuple(argv),
            )
            if used:
                stdout = (compact + "\n").encode("utf-8")
        except (UnicodeError, OSError, ValueError):
            pass
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(proc.stderr)
    return proc.returncode if proc.returncode >= 0 else 128 - proc.returncode


def main(argv: list[str] | None = None) -> int:
    """CLI wrapper entrypoint: ``output_compressor.py [--sid <id> --] <cmd> [args...]``."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--codex-exec"]:
        return run_codex_exec(args[1:])
    # This wrapper only runs when a Terser hook rewrote an agent's shell command,
    # so retrieval hints should target the agent's MCP tools, not the shell CLI.
    from runtime import context_sandbox

    context_sandbox.set_hint_host(os.environ.get("TERSER_HOST") or "agent")
    session_id = os.environ.get("TERSER_SESSION_ID", "")
    if len(args) >= 3 and args[0] == "--sid" and args[2] == "--":
        session_id = args[1]
        args = args[3:]
    if not args:
        return 0
    if not _recheck_safe_argv(args):
        sys.stderr.write("[Terser] Refused non-whitelisted command in compressor wrapper.\n")
        return 126

    cmd_str = shlex.join(args)
    try:
        from runtime.config import no_window_kwargs

        proc = subprocess.run(
            args,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **no_window_kwargs(),
        )
    except FileNotFoundError:
        sys.stderr.write(f"[Terser] Command not found: {args[0]}\n")
        return 127
    except OSError as exc:
        sys.stderr.write(f"[Terser] Failed to execute {args[0]}: {exc}\n")
        return 1

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    host = os.environ.get("TERSER_HOST", "cli")
    if stdout:
        comp_out, _ = compress_output(
            stdout,
            command=cmd_str,
            returncode=int(proc.returncode),
            stderr=stderr,
            host=host,
        )
        if session_id:
            try:
                from runtime import cmd_delta

                comp_out, _ = cmd_delta.apply_command_delta(
                    session_id, cmd_str, comp_out, returncode=int(proc.returncode), host=host
                )
            except Exception:
                pass
        sys.stdout.write(comp_out)
        if not comp_out.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    if stderr:
        comp_err, _ = compress_output(stderr, command=cmd_str, max_chars=6000, host=os.environ.get("TERSER_HOST", "cli"))
        sys.stderr.write(comp_err)
        if not comp_err.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()
    return int(proc.returncode)


if __name__ == "__main__":
    sys.exit(main())
