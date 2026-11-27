"""Terser — SmartCrusher Statistical JSON & API Array Pruner (Reversible).

Features:
  1. Unwraps both top-level JSON arrays and common API envelopes (`items`, `results`,
     `data`, `entries`, `records`, `files`, `issues`, `nodes`, `events`).
  2. Factors out constant keys shared identically across all items into `_const`.
  3. Strips redundant null/empty values where safe.
  4. For 5..40 items: lossless columnar encoding (`_format: "columnar"`, `_const`, `_seq`, `cols`, `rows`;
     `_seq` columns hold start + row_index * step).
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
    if not keys:
        return None

    # Factor out keys whose value is identical across ALL items (and drop all-None/empty keys)
    # BEFORE checking column width cap so wide API payloads with many constant/null fields still fold.
    consts: dict[str, Any] = {}
    var_keys: list[str] = []
    for k in keys:
        first = items[0].get(k)
        if all(it.get(k) == first for it in items):
            if first not in (None, "", [], {}):
                consts[k] = first
        else:
            var_keys.append(k)

    if len(var_keys) > 30:
        return None

    # Lossless: integer columns that advance by a fixed step become {"start", "step"}
    # (row i holds start + i * step), e.g. sequential ids.
    seqs: dict[str, dict[str, int]] = {}
    if len(items) >= 5:
        for k in list(var_keys):
            vals = [it.get(k) for it in items]
            if not all(type(v) is int and k in it for v, it in zip(vals, items)):
                continue
            step = vals[1] - vals[0]
            if step and all(b - a == step for a, b in zip(vals, vals[1:])):
                seqs[k] = {"start": vals[0], "step": step}
                var_keys.remove(k)

    rows = [[it.get(k) for k in var_keys] for it in items]
    out: dict[str, Any] = {"_format": "columnar"}
    if consts:
        out["_const"] = consts
    if seqs:
        out["_seq"] = seqs
    out["cols"] = var_keys
    out["rows"] = rows
    return out


def _find_all_nested_arrays(
    obj: dict[str, Any],
    max_depth: int = 5,
    max_arrays: int = 4,
) -> list[tuple[list[str], list[dict[str, Any]]]]:
    """Locate up to `max_arrays` uniform arrays of >=5 dicts inside an API/GraphQL envelope."""
    found: list[tuple[list[str], list[dict[str, Any]]]] = []
    seen_keys: set[str] = set()

    # Prefer well-known envelope keys first at this level
    for ek in _ENVELOPE_KEYS:
        val = obj.get(ek)
        if isinstance(val, list) and len(val) >= 5 and all(isinstance(x, dict) for x in val):
            found.append(([ek], val))
            seen_keys.add(ek)
            if len(found) >= max_arrays:
                return found

    for k, v in obj.items():
        if k in seen_keys:
            continue
        if isinstance(v, list) and len(v) >= 5 and all(isinstance(x, dict) for x in v):
            found.append(([k], v))
            if len(found) >= max_arrays:
                return found
        elif max_depth > 1 and isinstance(v, dict):
            sub = _find_all_nested_arrays(v, max_depth=max_depth - 1, max_arrays=max_arrays - len(found))
            for path, arr in sub:
                found.append(([k, *path], arr))
                if len(found) >= max_arrays:
                    return found
    return found


def _find_nested_array(obj: dict[str, Any], max_depth: int = 5) -> tuple[list[str], list[dict[str, Any]]] | None:
    """Locate the first uniform array of >=5 dicts up to `max_depth` levels inside an API/GraphQL envelope."""
    hits = _find_all_nested_arrays(obj, max_depth=max_depth, max_arrays=1)
    return hits[0] if hits else None


_LOSSLESS_MEDIUM_MAX_ITEMS = 80
_LOSSLESS_MEDIUM_MAX_CHARS = 6144


def _crush_item_list(
    items: list[dict[str, Any]],
    *,
    is_ndjson: bool,
    raw_for_archive: str,
    command: str,
    allow_sample: bool = True,
    env: dict[str, str] | None,
) -> dict[str, Any] | None:
    total = len(items)
    if total <= _SAMPLE_THRESHOLD or not allow_sample:
        crushed = _factor_and_columnize(items)
        if crushed is None:
            return None
        if is_ndjson:
            crushed["_source"] = "ndjson"
        return crushed

    # Lossless medium-array path: if 41..80 short items fit compactly in columnar form,
    # keep 100% of items losslessly instead of sampling (zero follow-up query turns).
    if total <= _LOSSLESS_MEDIUM_MAX_ITEMS:
        lossless = _factor_and_columnize(items)
        if lossless is not None:
            if is_ndjson:
                lossless["_source"] = "ndjson"
            if len(json.dumps(lossless, separators=(",", ":"), default=str)) <= _LOSSLESS_MEDIUM_MAX_CHARS:
                return lossless

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
    aid = context_sandbox.store_output(raw_for_archive, command=command or "json_payload", env=env)
    if is_ndjson:
        crushed["_source"] = "ndjson"
    crushed["_total_items"] = total
    crushed["_kept_items"] = len(sampled)
    crushed["_kept_indices"] = kept_indices
    if error_count:
        crushed["_errors_preserved"] = error_count
    if aid:
        crushed["_retrieve"] = context_sandbox.retrieval_hint(aid, "query", env=env).strip("`")
    return crushed


def _replace_nested(obj: Any, path: list[str], replacement: Any) -> Any:
    if not path:
        return replacement
    if isinstance(obj, dict):
        # Preserve all sibling keys (including empty lists/dicts/nulls like `"errors": []`)
        # so semantic meaning is never altered during lossless/columnar crushing.
        out = dict(obj)
        out[path[0]] = _replace_nested(obj[path[0]], path[1:], replacement)
        return out
    return obj


def _try_parse_ndjson(stripped: str) -> list[dict[str, Any]] | None:
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if len(lines) < 5:
        return None
    if not all(ln.startswith("{") and ln.endswith("}") for ln in lines[:10]):
        return None
    items: list[dict[str, Any]] = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except (ValueError, json.JSONDecodeError):
            return None
        if not isinstance(obj, dict):
            return None
        items.append(obj)
    return items


def crush_json(
    text: str,
    *,
    command: str = "",
    allow_sample: bool = True,
    env: dict[str, str] | None = None,
) -> str | None:
    """Apply SmartCrusher columnar/statistical compression to large JSON, nested GraphQL, or NDJSON payloads.

    When `allow_sample=False`, only 100% lossless transforms (columnar factoring and JSON minification)
    are applied—never statistical sampling. Returns compressed JSON string if savings >= 20%, else None.
    """
    stripped = (text or "").strip()
    if len(stripped) < _MIN_CHARS or stripped[:1] not in ("[", "{"):
        return None

    data: Any = None
    is_ndjson = False
    try:
        data = json.loads(stripped)
    except (ValueError, json.JSONDecodeError):
        nd_items = _try_parse_ndjson(stripped)
        if nd_items is None:
            return None
        data = nd_items
        is_ndjson = True

    targets: list[tuple[list[str], list[dict[str, Any]]]] = []
    if isinstance(data, list) and len(data) >= 5 and all(isinstance(x, dict) for x in data):
        targets = [([], data)]
    elif isinstance(data, dict):
        targets = _find_all_nested_arrays(data, max_depth=5, max_arrays=4)

    if not targets:
        # Lossless minification fallback for large pretty-printed single JSON objects/arrays
        if not is_ndjson and len(stripped) >= 1500:
            minified = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
            if len(minified) < int(len(stripped) * 0.80):
                return minified
        return None

    result_obj: Any = data
    any_crushed = False
    for path, items in targets:
        crushed = _crush_item_list(
            items,
            is_ndjson=is_ndjson,
            raw_for_archive=stripped,
            command=command,
            allow_sample=allow_sample,
            env=env,
        )
        if crushed is None:
            continue
        any_crushed = True
        result_obj = _replace_nested(result_obj, path, crushed) if path else crushed

    if not any_crushed:
        return None

    encoded = json.dumps(result_obj, separators=(",", ":"), default=str)
    if len(encoded) < int(len(stripped) * 0.80):
        return encoded
    return None


def crush_mixed_ndjson(
    text: str,
    *,
    command: str = "",
    env: dict[str, str] | None = None,
) -> str | None:
    """Fold contiguous runs of >=5 JSON-object lines inside mixed stdout (e.g. server logs with banners)."""
    if not text or len(text) < _MIN_CHARS or "{" not in text:
        return None
    lines = text.splitlines()
    if len(lines) < 5:
        return None

    out: list[str] = []
    i, n = 0, len(lines)
    changed = False
    while i < n:
        s = lines[i].strip()
        if s.startswith("{") and s.endswith("}"):
            j = i
            items: list[dict[str, Any]] = []
            while j < n:
                cur_s = lines[j].strip()
                if not (cur_s.startswith("{") and cur_s.endswith("}")):
                    break
                try:
                    obj = json.loads(cur_s)
                except (ValueError, json.JSONDecodeError):
                    break
                if not isinstance(obj, dict):
                    break
                items.append(obj)
                j += 1
            if len(items) >= 5:
                raw_block = "\n".join(lines[i:j])
                crushed = crush_json(raw_block, command=command, env=env)
                if crushed is not None and len(crushed) < int(len(raw_block) * 0.85):
                    out.append(crushed)
                    changed = True
                    i = j
                    continue
            out.extend(lines[i : max(j, i + 1)])
            i = max(j, i + 1)
        else:
            out.append(lines[i])
            i += 1

    if not changed:
        return None
    rendered = "\n".join(out)
    return rendered if len(rendered) < int(len(text) * 0.85) else None
