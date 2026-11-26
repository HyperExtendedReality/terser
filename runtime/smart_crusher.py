"""Terser — SmartCrusher Statistical JSON & API Array Pruner (Reversible).

Features:
  1. Unwraps both top-level JSON arrays and common API envelopes (`items`, `results`,
     `data`, `entries`, `records`, `files`, `issues`, `nodes`, `events`).
  2. Factors out constant keys shared identically across all items into `_const`.
  3. Strips redundant null/empty values where safe.
  4. For 5..40 items: lossless columnar encoding (`_format: "columnar"`, `_const`, `cols`, `rows`).
  5. For >40 items: statistical SmartCrusher sampling + SQLite FTS5 archive:
       - First 3 items (schema/order)
       - Last 3 items (recency)
       - 100% of error / failed / warning / critical items (unconditional!)
       - Statistical numeric outliers (> 2.0 std dev from mean, e.g. latency/size spikes)
     Full raw JSON is indexed in `sandbox.db` for instant `terse query <id> "<term>"` retrieval.

Pure Python stdlib, fail-open, zero dependencies.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from runtime import context_sandbox

_ENVELOPE_KEYS = ("items", "results", "data", "entries", "records", "files", "issues", "nodes", "events", "hits")
_ERROR_RE = re.compile(r"(?i)\b(?:error|err|fail|failed|failure|exception|critical|fatal|panic|timeout|warn|warning)\b")
_MIN_CHARS = 1200
_SAMPLE_THRESHOLD = 40


def _is_error_item(item: dict[str, Any]) -> bool:
    for k, v in item.items():
        lk = str(k).lower()
        if lk in ("error", "err", "exception", "traceback") and v not in (None, False, "", [], {}):
            return True
        if lk in ("status", "state", "level", "severity", "outcome") and isinstance(v, str) and _ERROR_RE.search(v):
            return True
        if lk in ("ok", "success", "passed", "healthy") and v is False:
            return True
    return False


def _outlier_indices(items: list[dict[str, Any]], max_outliers: int = 8) -> set[int]:
    """Identify indices with numeric values > 2.0 standard deviations from the mean."""
    numeric_cols: dict[str, list[tuple[int, float]]] = {}
    for idx, item in enumerate(items):
        for k, v in item.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                # Skip monotonic IDs/indexes
                if str(k).lower() in ("id", "index", "idx", "line", "seq", "offset"):
                    continue
                numeric_cols.setdefault(k, []).append((idx, float(v)))

    scored: list[tuple[float, int]] = []
    for _col, pairs in numeric_cols.items():
        if len(pairs) < 10:
            continue
        vals = [v for _, v in pairs]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        std = math.sqrt(var)
        if std <= 1e-9:
            continue
        for idx, v in pairs:
            z = abs(v - mean) / std
            if z > 2.0:
                scored.append((z, idx))
    scored.sort(reverse=True)
    out: set[int] = set()
    for _z, idx in scored:
        out.add(idx)
        if len(out) >= max_outliers:
            break
    return out


def _factor_and_columnize(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    keys: list[str] = []
    seen: set[str] = set()
    for item in items:
        for k in item.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    if not keys or len(keys) > 30:
        return None

    # Factor out keys whose value is identical across ALL items (and drop all-None keys)
    consts: dict[str, Any] = {}
    var_keys: list[str] = []
    for k in keys:
        first = items[0].get(k)
        if all(it.get(k) == first for it in items):
            if first not in (None, "", [], {}):
                consts[k] = first
        else:
            var_keys.append(k)

    rows = [[it.get(k) for k in var_keys] for it in items]
    out: dict[str, Any] = {"_format": "columnar"}
    if consts:
        out["_const"] = consts
    out["cols"] = var_keys
    out["rows"] = rows
    return out


def crush_json(
    text: str,
    *,
    command: str = "",
    env: dict[str, str] | None = None,
) -> str | None:
    """Apply SmartCrusher columnar/statistical compression to large JSON payloads.

    Returns compressed JSON string if savings >= 20%, else None.
    """
    stripped = (text or "").strip()
    if len(stripped) < _MIN_CHARS or stripped[:1] not in ("[", "{"):
        return None
    try:
        data = json.loads(stripped)
    except (ValueError, json.JSONDecodeError):
        return None

    envelope_key: str | None = None
    envelope_meta: dict[str, Any] = {}
    items: list[Any] | None = None

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for ek in _ENVELOPE_KEYS:
            val = data.get(ek)
            if isinstance(val, list) and len(val) >= 5:
                envelope_key = ek
                items = val
                envelope_meta = {k: v for k, v in data.items() if k != ek and v not in (None, "", [], {})}
                break

    if not items or len(items) < 5 or not all(isinstance(x, dict) for x in items):
        return None

    total = len(items)
    if total <= _SAMPLE_THRESHOLD:
        crushed = _factor_and_columnize(items)
        if crushed is None:
            return None
        result_obj: Any = {**envelope_meta, envelope_key: crushed} if envelope_key else crushed
    else:
        # SmartCrusher statistical pruning + CCR FTS5 archive
        keep: set[int] = set(range(min(3, total))) | set(range(max(0, total - 3), total))
        error_count = 0
        for idx, it in enumerate(items):
            if _is_error_item(it):
                keep.add(idx)
                error_count += 1
        keep |= _outlier_indices(items)

        kept_indices = sorted(keep)
        sampled = [items[i] for i in kept_indices]
        crushed = _factor_and_columnize(sampled)
        if crushed is None:
            return None
        aid = context_sandbox.store_output(stripped, command=command or "json_payload", env=env)
        crushed["_total_items"] = total
        crushed["_kept_items"] = len(sampled)
        crushed["_kept_indices"] = kept_indices
        if error_count:
            crushed["_errors_preserved"] = error_count
        if aid:
            crushed["_retrieve"] = f'terse query {aid} "<keywords>"'
        result_obj = {**envelope_meta, envelope_key: crushed} if envelope_key else crushed

    encoded = json.dumps(result_obj, separators=(",", ":"), default=str)
    if len(encoded) < int(len(stripped) * 0.80):
        return encoded
    return None
