"""Caption (WebVTT / SRT) compaction that keeps every spoken word.

YouTube auto-captions repeat each line in the next cue as the text scrolls, wrap every word in
``<00:00:01.234><c>`` timing tags, and put a timestamp on every cue. This module removes only that
duplication and markup: each caption line is kept once, in order, and lines are grouped into
paragraphs that start with a ``[m:ss]`` timestamp (about one per 30 seconds). The raw file stays
available to callers that archive it.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_CUE_RE = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}")
_INLINE_TS_RE = re.compile(r"<\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}>")
_MARKUP_RE = re.compile(r"</?(?:c|v|lang|ruby|rt|i|b|u|font)(?:[ .][^>]*)?>", re.I)
_SENTENCE_END = (".", "?", "!", "…", '."', '?"', '!"')
_VTT_SKIP_BLOCKS = ("NOTE", "STYLE", "REGION")


@dataclass(frozen=True)
class Paragraph:
    start: float
    text: str


def _seconds(h: str | None, m: str, s: str, frac: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(3, "0")[:3]) / 1000


def format_ts(seconds: float) -> str:
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_ts(value: str) -> float | None:
    """Parse ``90``, ``1:30``, or ``1:02:03`` into seconds."""
    parts = value.strip().split(":")
    if not parts or len(parts) > 3 or not all(p.isdigit() for p in parts):
        return None
    total = 0
    for part in parts:
        total = total * 60 + int(part)
    return float(total)


def parse_range(value: str) -> tuple[float, float] | None:
    """Parse ``12:30-18:00`` (either end may be empty) into a (start, end) pair in seconds."""
    if "-" not in value:
        return None
    lo, hi = value.split("-", 1)
    start = parse_ts(lo) if lo.strip() else 0.0
    end = parse_ts(hi) if hi.strip() else float("inf")
    if start is None or end is None or end < start:
        return None
    return start, end


def looks_like_captions(text: str) -> bool:
    head = text.lstrip("﻿ \t\r\n")[:400]
    if head.startswith("WEBVTT"):
        return True
    lines = [ln for ln in head.splitlines() if ln.strip()][:4]
    return len(lines) >= 2 and lines[0].strip().isdigit() and bool(_CUE_RE.match(lines[1]))


def _parse_cues(text: str) -> list[tuple[float, list[str]]]:
    """Return ``(start_seconds, raw_lines)`` per cue. Raw lines keep their inline tags."""
    body = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[tuple[float, list[str]]] = []
    # Only an empty line ends a cue: YouTube puts whitespace-only lines inside cues.
    for block in re.split(r"\n\n+", body):
        lines = block.split("\n")
        if lines and lines[0].startswith(_VTT_SKIP_BLOCKS):
            continue
        for i, line in enumerate(lines):
            m = _CUE_RE.match(line)
            if m:
                cues.append((_seconds(*m.groups()[:4]), lines[i + 1:]))
                break
    return cues


def _clean(line: str) -> str:
    return " ".join(html.unescape(_MARKUP_RE.sub("", _INLINE_TS_RE.sub("", line))).split())


def caption_lines(text: str) -> list[tuple[float, str]] | None:
    """Every caption line once, with the start time of the cue where it first appears.

    In YouTube's scrolling format a line is new when it carries inline word timings; a line
    without them is the previous cue's text shown again. Without inline timings every line
    is kept, so plain SRT/VTT files lose nothing.
    """
    cues = _parse_cues(text)
    if not cues:
        return None
    rolling = bool(_INLINE_TS_RE.search(text))
    out: list[tuple[float, str]] = []
    for start, raw_lines in cues:
        for raw in raw_lines:
            line = _clean(raw)
            if not line:
                continue
            if rolling and not _INLINE_TS_RE.search(raw) and out and out[-1][1] == line:
                continue
            if rolling and not _INLINE_TS_RE.search(raw) and len(out) >= 2 and out[-2][1] == line:
                continue
            out.append((start, line))
    return out


def paragraphs(
    lines: list[tuple[float, str]],
    *,
    breaks: list[float] | None = None,
    target_s: float = 30.0,
    max_s: float = 60.0,
) -> list[Paragraph]:
    """Group lines into paragraphs of about ``target_s`` seconds, splitting at ``breaks`` (chapters)."""
    marks = sorted(b for b in (breaks or []) if b > 0)
    out: list[Paragraph] = []
    cur: list[str] = []
    cur_start = 0.0
    next_mark = 0
    for start, line in lines:
        crossed = False
        while next_mark < len(marks) and start >= marks[next_mark]:
            crossed = True
            next_mark += 1
        if cur:
            elapsed = start - cur_start
            sentence_done = cur[-1].endswith(_SENTENCE_END)
            if crossed or elapsed >= max_s or (elapsed >= target_s and sentence_done):
                out.append(Paragraph(cur_start, " ".join(cur)))
                cur = []
        if not cur:
            cur_start = start
        cur.append(line)
    if cur:
        out.append(Paragraph(cur_start, " ".join(cur)))
    return out


def render(paras: list[Paragraph], chapters: list[dict] | None = None) -> str:
    """One ``[m:ss] text`` line per paragraph, with ``## [m:ss] title`` before each chapter."""
    heads = sorted(
        (float(c.get("start_time") or 0), str(c.get("title") or "").strip())
        for c in (chapters or []) if isinstance(c, dict)
    )
    out: list[str] = []
    hi = 0
    for para in paras:
        while hi < len(heads) and heads[hi][0] <= para.start:
            out.append(f"## [{format_ts(heads[hi][0])}] {heads[hi][1]}")
            hi += 1
        out.append(f"[{format_ts(para.start)}] {para.text}")
    for start, title in heads[hi:]:
        out.append(f"## [{format_ts(start)}] {title}")
    return "\n".join(out)


def slice_paragraphs(paras: list[Paragraph], start: float, end: float) -> list[Paragraph]:
    """Paragraphs overlapping ``[start, end)``: the one in progress at ``start`` is included."""
    keep: list[Paragraph] = []
    for i, para in enumerate(paras):
        nxt = paras[i + 1].start if i + 1 < len(paras) else float("inf")
        if para.start < end and nxt > start:
            keep.append(para)
    return keep


def outline(paras: list[Paragraph], chapters: list[dict] | None = None, *, window_s: float = 300.0) -> str:
    """Per chapter (or per 5-minute window): time span, word count, and opening words."""
    if not paras:
        return ""
    spans: list[tuple[float, float, str]] = []
    heads = sorted(
        (float(c.get("start_time") or 0), str(c.get("title") or "").strip())
        for c in (chapters or []) if isinstance(c, dict)
    )
    last = paras[-1].start + 1
    if heads:
        for i, (start, title) in enumerate(heads):
            end = heads[i + 1][0] if i + 1 < len(heads) else last
            spans.append((start, end, title))
    else:
        t = 0.0
        while t < last:
            spans.append((t, t + window_s, ""))
            t += window_s
    out: list[str] = []
    for start, end, title in spans:
        part = slice_paragraphs(paras, start, end) if not heads else [p for p in paras if start <= p.start < end]
        if not part:
            continue
        words = sum(len(p.text.split()) for p in part)
        opening = " ".join(part[0].text.split()[:14])
        label = f" {title} —" if title else ""
        out.append(f"[{format_ts(start)}-{format_ts(min(end, last))}]{label} {words} words: {opening} …")
    return "\n".join(out)


def compact(text: str, *, chapters: list[dict] | None = None) -> tuple[str, list[Paragraph]] | None:
    """Return the compacted transcript text and its paragraphs, or None if ``text`` isn't captions."""
    if not looks_like_captions(text):
        return None
    lines = caption_lines(text)
    if not lines:
        return None
    breaks = [float(c.get("start_time") or 0) for c in (chapters or []) if isinstance(c, dict)]
    paras = paragraphs(lines, breaks=breaks)
    return render(paras, chapters), paras


def from_rendered(text: str) -> list[Paragraph]:
    """Rebuild paragraphs from :func:`render` output (chapter heading lines are skipped)."""
    out: list[Paragraph] = []
    for line in text.splitlines():
        m = re.match(r"^\[(\d+(?::\d{2}){1,2})\] (.*)$", line)
        if m:
            ts = parse_ts(m.group(1))
            if ts is not None:
                out.append(Paragraph(ts, m.group(2)))
    return out
