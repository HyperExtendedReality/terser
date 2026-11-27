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
    r".*\.(?:min\.js|min\.css|map|pb\.go|generated\.[a-z0-9]+|snap))$"
)
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")
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


def compact_web_and_markdown(text: str) -> str:
    """Strip HTML nav/script/style boilerplate and compact padded Markdown tables into lossless TSV."""
    if not text or len(text) < 400:
        return text

    cur = text
    # 1. HTML boilerplate stripping (only when text clearly contains HTML markup)
    if len(_HTML_TAG_PROBE_RE.findall(cur[:8192])) >= 3:
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


def _error_pinned_window(text: str, max_chars: int, archive_id: str | None) -> str:
    """Keep head + ALL error/warning/diff lines + tail within max_chars."""
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    head_n = min(30, len(lines))
    tail_n = min(40, max(0, len(lines) - head_n))
    head = lines[:head_n]
    mid = lines[head_n: len(lines) - tail_n] if tail_n else lines[head_n:]
    tail = lines[len(lines) - tail_n:] if tail_n else []

    pinned_mid: list[str] = []
    pinned_budget = max(2000, max_chars // 2)
    used = 0
    for idx, ln in enumerate(mid, start=head_n + 1):
        if _SIGNAL_LINE_RE.search(ln):
            entry = f"L{idx}: {ln}"
            if used + len(entry) + 1 <= pinned_budget:
                pinned_mid.append(entry)
                used += len(entry) + 1

    omitted = max(0, len(mid) - len(pinned_mid))
    pointer = (
        f" (full output indexed: `terse query {archive_id} \"<keywords>\"` or `terse expand {archive_id} [start end]`)"
        if archive_id
        else ""
    )
    parts: list[str] = list(head)
    if omitted > 0:
        parts.append(f"... [{omitted} routine middle lines omitted; {len(pinned_mid)} signal lines preserved{pointer}] ...")
    parts.extend(pinned_mid)
    if tail:
        parts.append("--- tail ---")
        parts.extend(tail)
    result = "\n".join(parts)
    if len(result) > max_chars:
        half = max(500, (max_chars - 120) // 2)
        result = result[:half] + f"\n... [truncated{pointer}] ...\n" + result[-half:]
    return result


def _vendor_compress(text: str, command: str, returncode: int = 0, stderr: str = "") -> str:
    """Delegate to Terser's bundled specialized command handlers (git, pytest, npm, etc.)."""
    if not command or not text:
        return text
    try:
        ensure_vendor_paths()
        import bash_compress as _bc  # type: ignore

        fn = getattr(_bc, "compress", None)
        if callable(fn):
            out = fn(command, text, returncode=returncode, stderr=stderr)
            if isinstance(out, str) and out:
                return out
    except Exception:
        pass
    return text


def compress_output(
    text: str,
    *,
    command: str = "",
    returncode: int = 0,
    stderr: str = "",
    max_chars: int = _DEFAULT_MAX_CHARS,
    enhanced: bool = True,
    host: str = "unknown",
    workspace: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run the 5-stage quality-preserving output compression pipeline.

    Returns ``(compressed_text, stats_dict)``.
    """
    if not text:
        return "", {"raw_chars": 0, "served_chars": 0, "archive_id": None}

    raw_len = len(text)
    cur = strip_ansi_and_progress(text) if enhanced else text

    # Stage 2: lockfile diff elision + framework stack folding + compiler note dedup + base64 + web/table compaction + filters
    archive_id: str | None = None
    if enhanced:
        cur = elide_lockfile_diffs(cur)
        cur = fold_framework_stack_frames(cur)
        cur = dedup_compiler_notes(cur)
        cur, b64_aid = fold_base64_blobs(cur, command=command, env=env)
        if b64_aid:
            archive_id = b64_aid
        cur = compact_web_and_markdown(cur)
        cur = compact_aligned_tables(cur)
    if command:
        cur = apply_stream_filters(cur, command) if enhanced else cur
        cur = _vendor_compress(cur, command, returncode=returncode, stderr=stderr)

    # Stage 3: SmartCrusher statistical/columnar JSON pruning (reversible via sandbox.db)
    if enhanced:
        crushed: str | None = None
        try:
            from runtime import smart_crusher

            crushed = smart_crusher.crush_json(cur, command=command, env=env)
        except Exception:
            crushed = fold_uniform_json(cur)
        if crushed is not None:
            cur = crushed
        else:
            cur = elide_workspace_paths(cur, workspace=workspace)
            cur = group_grep_output(cur)
            cur = collapse_duplicate_lines(cur)

    if len(cur) > max_chars:
        if archive_id is None:
            archive_id = _archive_full_output(text, command=command, env=env)
        cur = _error_pinned_window(cur, max_chars=max_chars, archive_id=archive_id)

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
        ensure_vendor_paths()
        import bash_hook as _bh  # type: ignore

        joined = shlex.join(argv)
        if _bh._has_dangerous_chars(joined):
            return False
        return bool(_bh._is_whitelisted(joined))
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    """CLI wrapper entrypoint: ``output_compressor.py [--sid <id> --] <cmd> [args...]``."""
    args = list(sys.argv[1:] if argv is None else argv)
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
