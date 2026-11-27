"""Terser — warm hook worker.

terser-fast hands delegated hook events to this long-lived process instead of starting a
fresh interpreter (and re-importing every runtime module) for each one. Each request carries
the hook's full environment, working directory and raw stdin; the worker installs them and
runs the unchanged ``unified_hook`` dispatch, so replies are identical to a cold run.

Protocol (one request per TCP connection on 127.0.0.1):
  client -> ``<json header>\\n<raw stdin bytes>`` then half-close
  worker -> ``0`` (refused, nothing ran: client runs cold) or ``1`` + the hook's stdout

Requests run one at a time because the environment and cwd are process-wide. A request is
refused, never run, when its token is wrong, its body was truncated, it waited past its
acknowledgement deadline (the client has already fallen back), or the runtime code on disk
changed since the worker started (the worker then exits so a fresh one picks up the edit).
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import socket
import time
from pathlib import Path
from typing import Any, Callable

from runtime.config import atomic_write_json, state_home

_RUNTIME_DIR = Path(__file__).resolve().parent
_IDLE_EXIT_S = 900.0
_MAX_LIFETIME_S = 4 * 3600.0
_MAX_REQUEST_BYTES = 6 * 1024 * 1024
_ACK_MARGIN_S = 0.25
_RECV_TIMEOUT_S = 2.0
_CODE_SIG_CHECK_INTERVAL_S = 2.0


def _code_signature() -> tuple[tuple[str, int, int], ...]:
    sig = []
    for p in sorted(_RUNTIME_DIR.glob("*.py")):
        try:
            st = p.stat()
        except OSError:
            continue
        sig.append((p.name, int(st.st_mtime_ns), int(st.st_size)))
    return tuple(sig)


def _recv_request(conn: socket.socket) -> bytes | None:
    chunks: list[bytes] = []
    total = 0
    while True:
        part = conn.recv(65536)
        if not part:
            return b"".join(chunks)
        total += len(part)
        if total > _MAX_REQUEST_BYTES:
            return None
        chunks.append(part)


def _parse_request(data: bytes | None, token: str) -> tuple[dict[str, Any], bytes] | None:
    if not data:
        return None
    header_raw, sep, body = data.partition(b"\n")
    if not sep:
        return None
    try:
        req = json.loads(header_raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(req, dict) or not isinstance(req.get("env"), dict):
        return None
    if not hmac.compare_digest(str(req.get("token") or ""), token):
        return None
    if req.get("len") != len(body):
        return None
    try:
        if time.time() > float(req.get("deadline") or 0) - _ACK_MARGIN_S:
            return None
    except (TypeError, ValueError):
        return None
    return req, body


def _install_context(req: dict[str, Any]) -> None:
    env = {str(k): str(v) for k, v in req["env"].items()}
    cur = os.environ
    for k in list(cur.keys()):
        if k not in env:
            del cur[k]
    for k, v in env.items():
        if cur.get(k) != v:
            cur[k] = v
    cwd = str(req.get("cwd") or "")
    if cwd:
        try:
            if os.getcwd() != cwd:
                os.chdir(cwd)
        except OSError:
            pass


def serve(
    key: str,
    run_hook: Callable[[str, dict[str, Any], str | None], str],
    parse_payload: Callable[[bytes], dict[str, Any]],
) -> int:
    key = re.sub(r"[^0-9a-fA-F]", "", key or "")[:32]
    if not key:
        return 0
    token = secrets.token_hex(16)
    signature = _code_signature()
    info_path = state_home() / f"hook-worker-{key}.json"

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", 0))
        srv.listen(64)
        srv.settimeout(_IDLE_EXIT_S)
        atomic_write_json(info_path, {
            "port": srv.getsockname()[1],
            "token": token,
            "pid": os.getpid(),
            "started": time.time(),
        })
        started = time.time()
        last_sig_check = started
        while time.time() - started < _MAX_LIFETIME_S:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                break
            with conn:
                conn.settimeout(_RECV_TIMEOUT_S)
                try:
                    parsed = _parse_request(_recv_request(conn), token)
                except OSError:
                    continue
                now = time.time()
                stale = False
                if now - last_sig_check >= _CODE_SIG_CHECK_INTERVAL_S:
                    last_sig_check = now
                    stale = _code_signature() != signature
                if parsed is None or stale:
                    try:
                        conn.sendall(b"0")
                    except OSError:
                        pass
                    if stale:
                        break
                    continue
                req, body = parsed
                try:
                    conn.sendall(b"1")
                except OSError:
                    continue  # Client gone before the ack: it runs cold, so we must not run.
                _install_context(req)
                host = req.get("host") if isinstance(req.get("host"), str) else None
                out = run_hook(str(req.get("event") or ""), parse_payload(body), host)
                try:
                    conn.settimeout(10.0)
                    conn.sendall(out.encode("utf-8", errors="replace"))
                except OSError:
                    pass
    finally:
        try:
            srv.close()
        except OSError:
            pass
        try:
            current = json.loads(info_path.read_text(encoding="utf-8"))
            if isinstance(current, dict) and current.get("token") == token:
                info_path.unlink()
        except (OSError, ValueError):
            pass
    return 0
