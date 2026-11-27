"""YouTube transcript ingestion for Claude Code and Codex.

A YouTube watch page does not contain its transcript, so a plain web fetch returns little more than
the title. This module asks the user's own ``yt-dlp`` for the caption track and metadata, compacts
the captions with :mod:`runtime.captions` (every word kept), caches the result by video ID, and
indexes the transcript into the FTS5 sandbox for search.

Network access happens only through ``yt-dlp`` and only when the agent itself asked for that video
(Claude: an approved WebFetch of the URL; Codex: the agent running ``terse youtube``).
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from runtime import captions
from runtime.config import atomic_write_json, state_home

_ID = r"([A-Za-z0-9_-]{11})"
_URL_RE = re.compile(
    r"(?:https?://)?(?:[a-z0-9-]+\.)?(?:"
    r"youtube\.com/(?:watch\?(?:[^\s#\"'<>]*?&)?v=|shorts/|live/|embed/|v/)"
    r"|youtu\.be/|youtube-nocookie\.com/embed/)" + _ID,
    re.I,
)
_BARE_ID_RE = re.compile(rf"^{_ID}$")
_LANG_PREF = ("en", "en-US", "en-GB", "en-CA", "en-AU", "en-orig")
_DESCRIPTION_MAX = 3000
_FETCH_TIMEOUT_S = 45


def video_id(value: str) -> str | None:
    """The 11-character video ID in a YouTube URL (or a bare ID)."""
    if not isinstance(value, str):
        return None
    m = _URL_RE.search(value)
    if m:
        return m.group(1)
    m = _BARE_ID_RE.match(value.strip())
    return m.group(1) if m else None


def find_url(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    return m.group(0) if m else None


def ytdlp_command() -> list[str] | None:
    exe = shutil.which("yt-dlp") or shutil.which("yt_dlp")
    if exe:
        return [exe]
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    return None


def _video_dir(env: dict[str, str] | None) -> Path:
    return state_home(env) / "video"


def _cache_path(vid: str, env: dict[str, str] | None) -> Path:
    return _video_dir(env) / f"{vid}.json"


def load_cached(vid: str, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    try:
        data = json.loads(_cache_path(vid, env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("id") == vid else None


def _pick_track(files: list[Path], vid: str, manual: set[str]) -> tuple[Path, str, bool] | None:
    by_lang: dict[str, Path] = {}
    for f in files:
        # <id>.<lang>.<ext>
        stem = f.name[len(vid) + 1:]
        lang = stem.rsplit(".", 1)[0]
        if lang and lang != "live_chat":
            by_lang[lang] = f
    if not by_lang:
        return None

    def rank(lang: str) -> tuple[int, int, str]:
        pref = _LANG_PREF.index(lang) if lang in _LANG_PREF else (len(_LANG_PREF) if lang.startswith("en") else 99)
        return (0 if lang in manual else 1, pref, lang)

    lang = min(by_lang, key=rank)
    return by_lang[lang], lang, lang in manual


def _run_ytdlp(vid: str, out_dir: Path, timeout: float) -> str:
    cmd = ytdlp_command()
    if not cmd:
        return "yt-dlp is not installed (install it with `pip install yt-dlp` or your package manager)"
    args = [
        *cmd, "--skip-download", "--write-info-json", "--write-subs", "--write-auto-subs",
        "--sub-langs", "en,en-US,en-GB,en-orig,.*-orig", "--sub-format", "vtt/srt/best",
        "--no-playlist", "--no-warnings", "--no-progress", "--ignore-errors",
        "-o", str(out_dir / "%(id)s.%(ext)s"), f"https://www.youtube.com/watch?v={vid}",
    ]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"yt-dlp timed out after {int(timeout)} s"
    except OSError as exc:
        return f"yt-dlp could not start: {exc}"
    if proc.returncode != 0 and not any(out_dir.glob(f"{vid}*")):
        tail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[-1:] or ["no output"]
        return f"yt-dlp failed: {tail[0][:240]}"
    return ""


def fetch(
    vid: str,
    *,
    env: dict[str, str] | None = None,
    refresh: bool = False,
    timeout: float = _FETCH_TIMEOUT_S,
) -> dict[str, Any]:
    """Return cached or freshly fetched metadata + transcript, or ``{"error": ...}``."""
    if not refresh:
        cached = load_cached(vid, env)
        if cached:
            return cached
    vdir = _video_dir(env)
    vdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=vdir, prefix="tmp-") as tmp:
        out_dir = Path(tmp)
        err = _run_ytdlp(vid, out_dir, timeout)
        if err:
            return {"error": err}
        info: dict[str, Any] = {}
        info_path = out_dir / f"{vid}.info.json"
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        subs = [p for p in out_dir.glob(f"{vid}.*") if p.suffix.lower() in (".vtt", ".srt")]
        manual = set((info.get("subtitles") or {}).keys()) if isinstance(info.get("subtitles"), dict) else set()
        picked = _pick_track(subs, vid, manual)
        chapters = [
            {"start_time": float(c.get("start_time") or 0), "title": str(c.get("title") or "")}
            for c in (info.get("chapters") or []) if isinstance(c, dict)
        ]
        data: dict[str, Any] = {
            "id": vid,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": str(info.get("title") or ""),
            "channel": str(info.get("channel") or info.get("uploader") or ""),
            "upload_date": str(info.get("upload_date") or ""),
            "duration": info.get("duration") if isinstance(info.get("duration"), (int, float)) else None,
            "description": str(info.get("description") or ""),
            "chapters": chapters,
            "fetched_at": time.time(),
        }
        if picked:
            path, lang, is_manual = picked
            raw = path.read_text(encoding="utf-8", errors="replace")
            result = captions.compact(raw, chapters=chapters)
            if result:
                transcript, paras = result
                raw_copy = vdir / f"{vid}.{lang}{path.suffix.lower()}"
                raw_copy.write_text(raw, encoding="utf-8")
                data.update({
                    "lang": lang,
                    "source": "manual" if is_manual else "auto-generated",
                    "transcript": transcript,
                    "words": sum(len(p.text.split()) for p in paras),
                    "raw_chars": len(raw),
                    "raw_path": str(raw_copy),
                })
                try:
                    from runtime import context_sandbox

                    data["archive_id"] = context_sandbox.store_output(
                        transcript, command=f"youtube {vid} {data['title'][:60]}", env=env,
                    )
                except Exception:
                    pass
    if not data.get("title") and not data.get("transcript"):
        return {"error": "yt-dlp returned no metadata or captions for this video"}
    atomic_write_json(_cache_path(vid, env), data)
    return data


def cli_prefix() -> str:
    """How an agent shell should invoke the Terser CLI."""
    if shutil.which("terse"):
        return "terse"
    cli = Path(__file__).resolve().parent / "cli.py"
    return f'python "{cli}"'


def _header(data: dict[str, Any]) -> list[str]:
    bits = []
    if data.get("channel"):
        bits.append(f"Channel: {data['channel']}")
    date = str(data.get("upload_date") or "")
    if len(date) == 8 and date.isdigit():
        bits.append(f"Uploaded: {date[:4]}-{date[4:6]}-{date[6:]}")
    if data.get("duration"):
        bits.append(f"Duration: {captions.format_ts(float(data['duration']))}")
    if data.get("transcript"):
        bits.append(f"Captions: {data.get('source')} ({data.get('lang')}), {data.get('words')} words")
    out = [f"[Terser] YouTube video: {data.get('title') or data['id']} ({data['url']})"]
    if bits:
        out.append(" · ".join(bits))
    desc = str(data.get("description") or "").strip()
    if desc:
        if len(desc) > _DESCRIPTION_MAX:
            desc = desc[:_DESCRIPTION_MAX].rstrip() + f"\n… [description continues: `{cli_prefix()} youtube {data['id']} --description`]"
        out += ["Description:", desc]
    if data.get("chapters"):
        out.append("Chapters:")
        out += [f"  [{captions.format_ts(c['start_time'])}] {c['title']}" for c in data["chapters"]]
    return out


def render(data: dict[str, Any], *, inline_max_chars: int = 80000) -> str:
    """Metadata plus the full transcript, or an outline when the transcript exceeds ``inline_max_chars``."""
    vid = data["id"]
    cli = cli_prefix()
    out = _header(data)
    transcript = str(data.get("transcript") or "")
    if not transcript:
        out.append("No captions are available for this video, so there is no transcript.")
        return "\n".join(out)
    tools = (
        f"`{cli} youtube {vid} --range 12:30-18:00` (exact text of a time range) · "
        f"`{cli} youtube {vid} --query \"terms\"` (search) · `{cli} youtube {vid} --full` · "
        f"`{cli} youtube {vid} --raw` (original caption file)"
    )
    if len(transcript) <= inline_max_chars:
        out += [
            "Transcript (every caption word, one line per ~30 s, [m:ss] = start time):",
            transcript,
            f"More: {tools}",
        ]
    else:
        paras = captions.from_rendered(transcript)
        out += [
            f"The transcript is {len(transcript):,} characters, so here is an outline (word counts and opening words):",
            captions.outline(paras, data.get("chapters")),
            f"Get the text with {tools}",
        ]
    return "\n".join(out)


def transcript_range(data: dict[str, Any], start: float, end: float) -> str:
    paras = captions.slice_paragraphs(captions.from_rendered(str(data.get("transcript") or "")), start, end)
    return captions.render(paras, [c for c in data.get("chapters") or [] if start <= c["start_time"] < end])


# --- Codex: steer page/JSON fetches of a video to the transcript command ----------------------------

_FETCHER_RE = re.compile(
    r"(?:^|[\s;&|(])(?:curl|wget|iwr|irm|invoke-webrequest|invoke-restmethod|http|https|xh|lynx|w3m)(?:\.exe)?\s",
    re.I,
)
_YTDLP_DUMP_RE = re.compile(
    r"(?:^|[\s;&|(])(?:yt-dlp|yt_dlp|youtube-dl)(?:\.exe)?\s(?=.*(?:\s-[jJ]\b|--dump-json|--dump-single-json|--print\b))",
    re.I,
)


def command_hint(command: str) -> str | None:
    """For a shell command that downloads a YouTube page or its JSON, a pointer to ``terse youtube``."""
    if not isinstance(command, str):
        return None
    url = find_url(command)
    if not url or not (_FETCHER_RE.search(command) or _YTDLP_DUMP_RE.search(command)):
        return None
    vid = video_id(url)
    return (
        f"[Terser] A YouTube page or its yt-dlp JSON is mostly player code and format lists, and the page has no "
        f"transcript. `{cli_prefix()} youtube {vid}` prints the title, description, chapters and the full "
        "transcript (every caption word with timestamps); add `--range 12:30-18:00` or `--query \"terms\"` for part "
        "of it. Retry the same command to run it as written."
    )
