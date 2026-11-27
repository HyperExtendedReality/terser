"""``fetch_and_index``: fetch a page once, keep it searchable, return only what was asked.

Works on every host, because it is a ``terser-index`` action rather than a hook on the
host's own fetch tool. The page is converted to Markdown-ish text (headings kept),
archived in ``~/.terser/sandbox.db`` (secrets redacted, 7-day TTL), and the reply is the
heading outline with archive line numbers plus either the BM25 sections matching
``query`` or the first part of the page. Follow-ups use ``search_sandbox`` /
``expand_sandbox`` with the returned ``archive_id`` and cost no new request.

This is the one Terser feature that makes a network request, and only when the agent
calls it with a URL. http(s) only, 20 s timeout, 5 MB cap, no cookies or credentials.
"""
from __future__ import annotations

import html as _html
import re
from typing import Any

_MAX_BYTES = 5 * 1024 * 1024
_TIMEOUT_S = 20
_HEAD_CHARS = 3000
_HEADING_TAG_RE = re.compile(r"(?is)<h([1-6])\b[^>]*>(.*?)</h\1\s*>")
_PRE_RE = re.compile(r"(?is)<pre\b[^>]*>(.*?)</pre\s*>")
_TAG_RE = re.compile(r"<[^>]+>")
_MD_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")


def _to_text(body: str, content_type: str) -> str:
    from runtime import output_compressor

    if "html" not in content_type and not re.search(r"(?i)<html|<body|<div|<p\b", body[:4096]):
        return body
    body = _HEADING_TAG_RE.sub(
        lambda m: "\n<p>" + "#" * int(m.group(1)) + " " + " ".join(_html.unescape(_TAG_RE.sub("", m.group(2))).split()) + "</p>\n",
        body,
    )
    body = _PRE_RE.sub(lambda m: "\n<p>```\n" + _html.unescape(_TAG_RE.sub("", m.group(1))) + "\n```</p>\n", body)
    return output_compressor.compact_web_and_markdown(body)


def _outline(text: str, limit: int = 50) -> list[str]:
    out = []
    for no, line in enumerate(text.splitlines(), start=1):
        m = _MD_HEADING_RE.match(line)
        if m:
            out.append(f"L{no} {'  ' * (len(m.group(1)) - 1)}{m.group(2)[:100]}")
            if len(out) >= limit:
                out.append("...")
                break
    return out


def fetch_and_index(args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    from runtime import context_sandbox
    from runtime.config import record_savings

    url = str(args.get("url") or "").strip()
    if not re.match(r"(?i)^https?://[^\s/]+", url):
        return {"error": "Provide an http(s) `url`."}
    req = urllib.request.Request(url, headers={"User-Agent": "terser-fetch/1.2", "Accept": "text/html,text/plain,text/markdown,application/json;q=0.9,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:  # noqa: S310 - scheme checked above
            raw = resp.read(_MAX_BYTES + 1)
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            charset = resp.headers.get_content_charset() or "utf-8"
            final_url = resp.geturl()
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code} for {url}"}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"error": f"Fetch failed for {url}: {exc}"}
    truncated = len(raw) > _MAX_BYTES
    try:
        body = raw[:_MAX_BYTES].decode(charset, errors="replace")
    except LookupError:
        body = raw[:_MAX_BYTES].decode("utf-8", errors="replace")
    text = _to_text(body, ctype)
    aid = context_sandbox.store_output(text, command=f"web:{final_url[:180]}", env=env)
    if not aid:
        return {"error": "Fetched, but archiving into sandbox.db failed."}
    query = str(args.get("query") or "").strip()
    limit = max(1, min(10, int(args.get("limit") or 4)))
    matched = context_sandbox.query_output(aid, query, limit=limit, env=env) if query else None
    if matched:
        body_out = matched
    else:
        body_out = text[:_HEAD_CHARS]
        cut = body_out.rfind("\n")
        if len(text) > _HEAD_CHARS and cut > _HEAD_CHARS // 2:
            body_out = body_out[:cut]
    served_len = len(body_out) + 400
    if len(raw) > served_len:
        record_savings("web_archive", len(raw), served_len, host="mcp", detail=final_url[:60], env=env)
    return {
        "url": final_url,
        "archive_id": aid,
        "lines": len(text.splitlines()),
        "chars": len(text),
        "truncated": truncated,
        "outline": _outline(text),
        "body": body_out,
        "matched": bool(matched),
        "hint": context_sandbox.retrieval_hint(aid, env=env),
    }


def format_response(p: dict[str, Any]) -> str:
    head = f"{p['url']} -> archive {p['archive_id']} ({p['lines']} lines, {p['chars']} chars{', truncated at 5 MB' if p.get('truncated') else ''})"
    parts = [head]
    if p.get("outline"):
        parts.append("Sections:\n" + "\n".join(p["outline"]))
    parts.append(("Matching sections:\n" if p.get("matched") else "Start of page:\n") + str(p.get("body") or ""))
    parts.append(str(p.get("hint") or ""))
    return "\n\n".join(x for x in parts if x)
