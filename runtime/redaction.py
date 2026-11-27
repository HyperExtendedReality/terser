"""Terser native secret/credential redaction for anything persisted to disk.

``redact(text, command=False)`` replaces credentials with ``[REDACTED]`` while keeping
the surrounding key name so archived output stays readable. With ``command=True`` it
also masks password-style command-line arguments.
"""
from __future__ import annotations

import re

_R = "[REDACTED]"

# Whole-token secrets with recognizable prefixes.
_TOKEN_RE = re.compile(
    r"(?:"
    r"sk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}"
    r"|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|glpat-[A-Za-z0-9_-]{20,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|ya29\.[0-9A-Za-z_-]{20,}"
    r"|xox[abposr]-[A-Za-z0-9-]{10,}"
    r"|hf_[A-Za-z0-9]{30,}"
    r"|npm_[A-Za-z0-9]{36}"
    r"|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)
_PEM_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_AUTH_HEADER_RE = re.compile(r"(?i)\b(authorization|proxy-authorization)(\s*[:=]\s*)(?:(bearer|basic|token)\s+)?[^\s'\"]+")
_URL_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+(@)")
_KV_RE = re.compile(
    r"(?i)\b((?:[a-z0-9_.-]*?)(?:api[_-]?key|secret(?:[_-]?key)?|access[_-]?key|auth[_-]?token|"
    r"access[_-]?token|refresh[_-]?token|client[_-]?secret|private[_-]?key|password|passwd|pwd)"
    r"[a-z0-9_]*)(\s*[:=]\s*|\"\s*:\s*\")(['\"]?)([^\s'\",;]{4,})"
)
_CMD_PASS_RE = re.compile(
    r"(?i)(--(?:password|passwd|token|api-key|secret|auth)(?:=|\s+))(['\"]?)[^\s'\"]+|(\s-p)([^\s-][^\s]*)"
)


def redact(text: str, *, command: bool = False) -> str:
    if not text:
        return ""
    out = _PEM_RE.sub(_R, text)
    out = _AUTH_HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{(m.group(3) + ' ') if m.group(3) else ''}{_R}", out)
    out = _URL_CRED_RE.sub(lambda m: f"{m.group(1)}{_R}{m.group(2)}", out)
    out = _KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{_R}", out)
    out = _TOKEN_RE.sub(_R, out)
    if command:
        out = _CMD_PASS_RE.sub(
            lambda m: f"{m.group(1)}{m.group(2) or ''}{_R}" if m.group(1) else f"{m.group(3)}{_R}", out
        )
    return out
