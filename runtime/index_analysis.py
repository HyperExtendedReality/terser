"""Repository analysis actions for ``terser-index`` (reached through ``order``).

Every action reads the stored index plus the files on disk (and git, where noted) and
returns a compact text answer, so the agent gets a conclusion instead of raw files.

get_hotspots            Files ranked by recent churn x size (git log), the usual bug nests
get_symbol_history      Commits that touched one symbol's lines (git log -L)
get_untested_symbols    Functions/classes whose name never appears in any test file
get_class_hierarchy     Ancestors and descendants of a class/interface/trait
find_implementations    Every class or impl that extends/implements a type
find_similar_symbols    Near-duplicate functions (MinHash over token shingles)
get_symbol_diff         One symbol's change between a git ref and the working tree
get_pr_risk_profile     Changed symbols scored by blast radius, test coverage, churn, size
get_layer_violations    Imports that break a declared layer order
list_workspaces         Monorepo members (npm/pnpm/yarn, Cargo, go.work, uv) with index counts
import_scip             Load a SCIP index (protobuf) for compiler-precise references

Project config: ``<repo>/.terser/config.toml`` may set

    [index]
    skip = ["generated/**", "*.pb.go"]   # never index these paths
    extensions = [".foo"]                # extra extensions (generic symbol regex)
    [analysis]
    test_globs = ["spec/**"]             # extra test-file patterns
    [[layers]]                           # top layer first; lower layers must not import higher
    name = "ui"
    paths = ["src/ui/**"]

Stdlib only. SCIP is decoded with a minimal protobuf wire reader.
"""
from __future__ import annotations

import fnmatch
import hashlib
import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

ANALYSIS_ACTIONS = frozenset({
    "get_hotspots", "get_symbol_history", "get_untested_symbols", "get_class_hierarchy",
    "find_implementations", "find_similar_symbols", "get_symbol_diff", "get_pr_risk_profile",
    "get_layer_violations", "list_workspaces", "import_scip",
})

_CALLABLE_KINDS = frozenset({
    "function", "def", "fn", "func", "method", "class", "struct", "interface", "trait", "enum",
    "defp", "proc", "sub", "subroutine", "procedure", "contract", "macro",
})
_CLASS_KINDS = frozenset({"class", "interface", "struct", "trait", "impl", "protocol", "record", "object", "contract", "type"})
_TOKEN_RE = re.compile(r"[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|==|!=|<=|>=|->|=>|&&|\|\||[^\s\w]")
_STR_TOKEN_RE = re.compile(r"^[\"']")
_NUM_TOKEN_RE = re.compile(r"^\d")
_MINHASH_N = 32
_BANDS = 8
_ROWS = _MINHASH_N // _BANDS
_MERSENNE = (1 << 61) - 1

_cfg_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def project_config(root: str | Path) -> dict[str, Any]:
    """Parsed ``<root>/.terser/config.toml`` (empty dict when absent or invalid)."""
    p = Path(root) / ".terser" / "config.toml"
    try:
        st = p.stat()
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    hit = _cfg_cache.get(str(p))
    if hit and hit[0] == key:
        return hit[1]
    try:
        from runtime import user_filters

        data = user_filters.parse_toml(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        data = {}
    data = data if isinstance(data, dict) else {}
    _cfg_cache[str(p)] = (key, data)
    return data


def index_rules(root: str | Path) -> tuple[list[str], set[str]]:
    """(skip globs, extra extensions) from the project config."""
    idx = project_config(root).get("index") or {}
    if not isinstance(idx, dict):
        return [], set()
    skip = [str(g) for g in idx.get("skip") or [] if str(g).strip()]
    exts = {e if str(e).startswith(".") else "." + str(e) for e in idx.get("extensions") or [] if str(e).strip()}
    return skip, {e.lower() for e in exts}


def glob_match(rel: str, pattern: str) -> bool:
    pat = pattern.lstrip("/")
    if fnmatch.fnmatchcase(rel, pat):
        return True
    if pat.endswith("/**") and (rel == pat[:-3] or rel.startswith(pat[:-3] + "/")):
        return True
    if pat.startswith("**/") and fnmatch.fnmatchcase(rel, pat[3:]):
        return True
    return "/" not in pat and fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pat)


def _ctx(args: dict[str, Any], env: dict[str, str] | None) -> Any:
    from runtime import index_mcp

    ctx = index_mcp._graph_ctx(args, env)
    if isinstance(ctx, dict):
        return ctx
    prefix = str(args.get("path_prefix") or "").strip().strip("/")
    if prefix:
        ctx.files = [f for f in ctx.files if f == prefix or f.startswith(prefix + "/")]
        ctx.symbols = [s for s in ctx.symbols if str(s.get("file", "")).startswith(prefix + "/") or s.get("file") == prefix]
    return ctx


def _is_test(rel: str, extra_globs: list[str]) -> bool:
    from runtime import index_mcp

    return index_mcp._is_test_path(rel) or any(glob_match(rel, g) for g in extra_globs)


def _test_globs(ctx: Any) -> list[str]:
    an = project_config(ctx.root).get("analysis") or {}
    return [str(g) for g in (an.get("test_globs") or [])] if isinstance(an, dict) else []


def _body(ctx: Any, sym: dict[str, Any]) -> str:
    text = ctx.text(str(sym.get("file") or "")) or ""
    lines = text.splitlines()
    s, e = int(sym.get("start_line") or 1), int(sym.get("end_line") or 1)
    return "\n".join(lines[s - 1:e])


def _git(root: Path, argv: list[str], timeout: int = 20) -> str | None:
    import subprocess

    from runtime.config import no_window_kwargs

    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *argv], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, **no_window_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _find_symbol(ctx: Any, ref: str) -> dict[str, Any] | None:
    from runtime import index_mcp

    for s in ctx.symbols:
        if s.get("id") == ref or s.get("qualified_name") == ref:
            return s
    name = index_mcp._simple_name(ref)
    named = ctx.defs_named(name) if name else []
    return named[0] if named else None


def _churn(ctx: Any, days: int) -> dict[str, int]:
    out = _git(ctx.root, ["log", f"--since={int(days)}.days", "--no-merges", "--name-only", "--format=%x00", "--relative"])
    counts: dict[str, int] = {}
    if not out:
        return counts
    for chunk in out.split("\0"):
        for rel in {ln.strip() for ln in chunk.splitlines() if ln.strip()}:
            counts[rel] = counts.get(rel, 0) + 1
    return counts


def _test_words(ctx: Any) -> set[str]:
    from runtime import index_mcp

    globs = _test_globs(ctx)
    words: set[str] = set()
    for rel in ctx.files:
        if _is_test(rel, globs):
            words.update(index_mcp._IDENT_RE.findall(ctx.text(rel) or ""))
    return words


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def get_hotspots(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    days = max(1, min(3650, int(args.get("days") or 90)))
    limit = max(1, min(100, int(args.get("limit") or 20)))
    churn = _churn(ctx, days)
    if not churn:
        return {"error": "No git history in the window (not a git repo, or no commits)."}
    sym_count: dict[str, int] = {}
    for s in ctx.symbols:
        sym_count[str(s.get("file"))] = sym_count.get(str(s.get("file")), 0) + 1
    rows = []
    indexed = set(ctx.files)
    for rel, n in churn.items():
        if rel not in indexed:
            continue
        loc = len((ctx.text(rel) or "").splitlines())
        score = n * (1 + math.log2(1 + loc)) * (1 + math.log2(1 + sym_count.get(rel, 0)) / 4)
        rows.append({"file": rel, "commits": n, "lines": loc, "symbols": sym_count.get(rel, 0), "score": round(score, 1)})
    rows.sort(key=lambda r: (-r["score"], r["file"]))
    return {"hotspots": rows[:limit], "days": days}


def get_symbol_history(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    sym = _find_symbol(ctx, str(args.get("symbol") or args.get("symbol_id") or ""))
    if sym is None:
        return {"error": "Symbol not found; pass `symbol` (name or id)."}
    limit = max(1, min(50, int(args.get("limit") or 10)))
    rng = f"{int(sym['start_line'])},{int(sym['end_line'])}:{sym['file']}"
    out = _git(ctx.root, ["log", f"-L{rng}", "--no-patch", f"-n{limit}", "--format=%h %ad %an: %s", "--date=short"], timeout=30)
    if out is None:
        return {"error": "`git log -L` failed (not a git repo, or the file is untracked)."}
    commits = [ln for ln in out.splitlines() if re.match(r"^[0-9a-f]{6,} ", ln)]
    return {"symbol": sym["id"], "commits": commits}


def get_untested_symbols(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    from runtime import index_mcp

    include_private = args.get("include_private") is True
    limit = max(1, min(500, int(args.get("limit") or 80)))
    globs = _test_globs(ctx)
    words = _test_words(ctx)
    if not words:
        return {"error": "No test files found in the index (test_*.py, *_test.go, *.spec.ts, tests/ ...)."}
    total = untested = 0
    by_file: dict[str, list[str]] = {}
    for s in ctx.symbols:
        rel = str(s.get("file") or "")
        if _is_test(rel, globs) or str(s.get("kind", "")).lower() not in _CALLABLE_KINDS:
            continue
        name = index_mcp._simple_name(str(s.get("name") or ""))
        if not name or name in index_mcp._DEAD_SKIP_NAMES or (name.startswith("_") and not include_private) or name.startswith("__"):
            continue
        total += 1
        if name in words:
            continue
        untested += 1
        by_file.setdefault(rel, []).append(f"{name} L{s.get('start_line')}")
    rows = []
    for rel, names in sorted(by_file.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        rows.append({"file": rel, "symbols": names})
        if sum(len(r["symbols"]) for r in rows) >= limit:
            break
    return {"untested": rows, "checked": total, "untested_count": untested}


_PY_BASES_RE = re.compile(r"^\s*class\s+(\w+)\s*\(([^)]*)\)")
_EXTENDS_RE = re.compile(r"\bclass\s+(\w+)(?:<[^>]*>)?[^{]*?\bextends\s+([\w.$<>, ]+?)(?:\s+implements\s+([\w.$<>, ]+))?\s*(?:\{|$)")
_IMPLEMENTS_RE = re.compile(r"\b(?:class|struct|enum|record)\s+(\w+)(?:<[^>]*>)?[^{]*?\bimplements\s+([\w.$<>, ]+?)\s*(?:\{|$)")
_IFACE_EXTENDS_RE = re.compile(r"\binterface\s+(\w+)(?:<[^>]*>)?\s+extends\s+([\w.$<>, ]+?)\s*(?:\{|$)")
_COLON_BASES_RE = re.compile(r"\b(?:class|struct|interface|protocol|record|object|enum)\s+(\w+)(?:<[^>]*>)?(?:\([^)]*\))?\s*:\s*([^{=]+?)\s*(?:\{|where\b|$)")
_RUST_IMPL_RE = re.compile(r"\bimpl(?:<[^>]*>)?\s+([\w:]+)(?:<[^>]*>)?\s+for\s+([\w:]+)")
_RUST_TRAIT_RE = re.compile(r"\btrait\s+(\w+)(?:<[^>]*>)?\s*:\s*([^{]+)")


def _split_bases(raw: str) -> list[str]:
    out = []
    depth = 0
    cur = ""
    for ch in raw:
        if ch in "<([":
            depth += 1
        elif ch in ">)]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    names = []
    for b in out:
        b = re.sub(r"<.*", "", b.strip())
        b = re.sub(r"^(?:public|private|protected|virtual|open)\s+", "", b)
        if not b or "=" in b:
            continue  # metaclass=..., keyword args
        names.append(b.split(".")[-1].split("::")[-1].strip())
    return [n for n in names if re.fullmatch(r"[A-Za-z_]\w*", n) and n not in ("object", "Any")]


def _edges(ctx: Any) -> list[tuple[str, str, str]]:
    """(child, parent, file) edges from class headers (first 3 lines of each class-like symbol)."""
    out: list[tuple[str, str, str]] = []
    for s in ctx.symbols:
        if str(s.get("kind", "")).lower() not in _CLASS_KINDS:
            continue
        rel = str(s.get("file") or "")
        lines = (ctx.text(rel) or "").splitlines()
        start = int(s.get("start_line") or 1)
        head = " ".join(ln.strip() for ln in lines[start - 1:start + 2])
        head = head.split("{", 1)[0] + ("{" if "{" in head else "")
        found = False
        if rel.endswith(".py"):
            m = _PY_BASES_RE.match(lines[start - 1] if start - 1 < len(lines) else "")
            if m:
                out.extend((m.group(1), b, rel) for b in _split_bases(m.group(2)))
            continue
        for rx in (_EXTENDS_RE,):
            m = rx.search(head)
            if m:
                found = True
                out.extend((m.group(1), b, rel) for b in _split_bases(m.group(2)))
                if m.group(3):
                    out.extend((m.group(1), b, rel) for b in _split_bases(m.group(3)))
        if not found:
            for rx in (_IMPLEMENTS_RE, _IFACE_EXTENDS_RE, _COLON_BASES_RE, _RUST_TRAIT_RE):
                m = rx.search(head)
                if m:
                    out.extend((m.group(1), b, rel) for b in _split_bases(m.group(2).replace("+", ",")))
                    break
        m = _RUST_IMPL_RE.search(head)
        if m:
            out.append((m.group(2).split("::")[-1], m.group(1).split("::")[-1], rel))
    return out


def get_class_hierarchy(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    from runtime import index_mcp

    name = index_mcp._simple_name(str(args.get("symbol") or args.get("name") or ""))
    if not name:
        return {"error": "Provide `symbol` (class, interface, or trait name)."}
    depth = max(1, min(6, int(args.get("depth") or 3)))
    edges = _edges(ctx)
    parents: dict[str, set[str]] = {}
    children: dict[str, set[str]] = {}
    where: dict[str, str] = {}
    for c, p, rel in edges:
        parents.setdefault(c, set()).add(p)
        children.setdefault(p, set()).add(c)
        where.setdefault(c, rel)
    for s in ctx.symbols:
        where.setdefault(index_mcp._simple_name(str(s.get("name") or "")), str(s.get("file") or ""))

    def walk(n: str, graph: dict[str, set[str]], d: int, seen: set[str]) -> list[dict[str, Any]]:
        if d > depth:
            return []
        out = []
        for m in sorted(graph.get(n, ())):
            if m in seen:
                continue
            out.append({"name": m, "file": where.get(m, "(external)"), "children": walk(m, graph, d + 1, seen | {m})})
        return out

    return {
        "symbol": name, "file": where.get(name, ""),
        "ancestors": walk(name, parents, 1, {name}),
        "descendants": walk(name, children, 1, {name}),
    }


def find_implementations(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    res = get_class_hierarchy(ctx, dict(args, depth=args.get("depth") or 6))
    if "error" in res:
        return res
    flat: list[str] = []

    def collect(nodes: list[dict[str, Any]], indent: int) -> None:
        for n in nodes:
            flat.append(f"{'  ' * indent}{n['name']} ({n['file']})")
            collect(n["children"], indent + 1)

    collect(res["descendants"], 0)
    return {"symbol": res["symbol"], "implementations": flat}


def _shingles(body: str, structural: bool, k: int = 5) -> set[int]:
    toks = []
    for t in _TOKEN_RE.findall(body):
        if _STR_TOKEN_RE.match(t):
            toks.append("S")
        elif _NUM_TOKEN_RE.match(t):
            toks.append("0")
        elif structural and (t[0].isalpha() or t[0] in "_$"):
            toks.append("v")
        else:
            toks.append(t)
    if len(toks) < k:
        return set()
    return {
        int.from_bytes(hashlib.blake2b(" ".join(toks[i:i + k]).encode(), digest_size=8).digest(), "big")
        for i in range(len(toks) - k + 1)
    }


_PERMS = [
    (int.from_bytes(hashlib.blake2b(f"a{i}".encode(), digest_size=8).digest(), "big") | 1,
     int.from_bytes(hashlib.blake2b(f"b{i}".encode(), digest_size=8).digest(), "big"))
    for i in range(_MINHASH_N)
]


def _minhash(sh: set[int]) -> tuple[int, ...]:
    return tuple(min((a * x + b) % _MERSENNE for x in sh) for a, b in _PERMS)


def find_similar_symbols(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    threshold = float(args.get("threshold") or 0.8)
    min_lines = max(3, int(args.get("min_lines") or 6))
    limit = max(1, min(100, int(args.get("limit") or 20)))
    structural = args.get("structural") is True
    target_ref = str(args.get("symbol") or args.get("symbol_id") or "")
    globs = _test_globs(ctx)
    include_tests = args.get("include_tests") is True
    cands = []
    for s in ctx.symbols:
        if str(s.get("kind", "")).lower() not in _CALLABLE_KINDS - {"class", "struct", "interface", "trait", "enum", "contract"}:
            continue
        if int(s.get("end_line") or 0) - int(s.get("start_line") or 0) + 1 < min_lines:
            continue
        if not include_tests and _is_test(str(s.get("file") or ""), globs):
            continue
        cands.append(s)
        if len(cands) >= 6000:
            break
    sh = {s["id"]: _shingles(_body(ctx, s), structural) for s in cands}
    sh = {k: v for k, v in sh.items() if len(v) >= 8}
    by_id = {s["id"]: s for s in cands}
    pairs: list[tuple[float, str, str]] = []
    if target_ref:
        tgt = _find_symbol(ctx, target_ref)
        if tgt is None:
            return {"error": f"Symbol `{target_ref}` not found."}
        tsh = sh.get(tgt["id"]) or _shingles(_body(ctx, tgt), structural)
        for sid, other in sh.items():
            if sid == tgt["id"] or not other:
                continue
            j = len(tsh & other) / len(tsh | other)
            if j >= threshold:
                pairs.append((j, tgt["id"], sid))
    elif len(sh) <= 400:
        ids = sorted(sh)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                j = len(sh[a] & sh[b]) / len(sh[a] | sh[b])
                if j >= threshold:
                    pairs.append((j, a, b))
    else:
        sigs = {sid: _minhash(v) for sid, v in sh.items()}
        buckets: dict[tuple[int, tuple[int, ...]], list[str]] = {}
        for sid, sig in sigs.items():
            for b in range(_BANDS):
                buckets.setdefault((b, sig[b * _ROWS:(b + 1) * _ROWS]), []).append(sid)
        seen: set[tuple[str, str]] = set()
        for ids in buckets.values():
            if len(ids) < 2 or len(ids) > 200:
                continue
            for i in range(len(ids)):
                for k in range(i + 1, len(ids)):
                    a, b = sorted((ids[i], ids[k]))
                    if (a, b) in seen:
                        continue
                    seen.add((a, b))
                    j = len(sh[a] & sh[b]) / len(sh[a] | sh[b])
                    if j >= threshold:
                        pairs.append((j, a, b))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    rows = []
    for j, a, b in pairs[:limit]:
        sa, sb = by_id.get(a) or _find_symbol(ctx, a), by_id.get(b)
        rows.append({
            "similarity": round(j, 2),
            "a": f"{sa['id']} ({sa['file']} L{sa['start_line']}-{sa['end_line']})" if sa else a,
            "b": f"{sb['id']} ({sb['file']} L{sb['start_line']}-{sb['end_line']})" if sb else b,
        })
    return {"pairs": rows, "compared": len(sh), "threshold": threshold}


def get_symbol_diff(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    import difflib

    from runtime import index_mcp

    ref = str(args.get("ref") or "HEAD").strip()
    if ref.startswith("-") or not index_mcp._GIT_REF_RE.match(ref):
        return {"error": f"Invalid git ref `{ref}`."}
    sym = _find_symbol(ctx, str(args.get("symbol") or args.get("symbol_id") or ""))
    if sym is None:
        return {"error": "Symbol not found; pass `symbol` (name or id)."}
    rel = str(sym["file"])
    new_body = _body(ctx, sym)
    old_text = _git(ctx.root, ["show", f"{ref}:{rel}"])
    if old_text is None:
        return {"symbol": sym["id"], "status": "new file (not in ref)", "diff": ""}
    if rel.endswith(".py"):
        old_syms = index_mcp._extract_python_symbols(rel, old_text)
    else:
        old_syms = index_mcp._extract_generic_symbols(rel, old_text)
    qn = sym.get("qualified_name") or sym.get("name")
    match = next((o for o in old_syms if o.get("qualified_name") == qn), None) or next(
        (o for o in old_syms if o.get("name") == sym.get("name") and o.get("kind") == sym.get("kind")), None
    )
    if match is None:
        return {"symbol": sym["id"], "status": f"new symbol (absent at {ref})", "diff": ""}
    old_lines = old_text.splitlines()[int(match["start_line"]) - 1:int(match["end_line"])]
    diff = list(difflib.unified_diff(
        old_lines, new_body.splitlines(), f"{ref}:{rel} L{match['start_line']}", f"working:{rel} L{sym['start_line']}", n=2, lineterm="",
    ))
    return {"symbol": sym["id"], "status": "unchanged" if not diff else "changed", "diff": "\n".join(diff)}


def get_pr_risk_profile(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    from runtime import index_mcp

    base = str(args.get("base") or args.get("ref") or "").strip()
    changed = index_mcp._action_get_changed_symbols(ctx, {"ref": base, "staged": args.get("staged") is True})
    if "error" in changed:
        return changed
    churn = _churn(ctx, 90)
    words = _test_words(ctx)
    globs = _test_globs(ctx)
    by_id = {s["id"]: s for s in ctx.symbols}
    rows = []
    for f in changed.get("files", []):
        rel = f["file"]
        for s in f.get("symbols", [])[:40]:
            full = by_id.get(s["id"]) or s
            name = index_mcp._simple_name(str(full.get("name") or s["id"].split("::")[-1].split("@")[0]))
            ref_files = len(index_mcp._references(ctx, name)) if name and len(name) > 2 else 0
            tested = _is_test(rel, globs) or (name in words)
            size = int(s["end_line"]) - int(s["start_line"]) + 1
            score = 2 * math.log2(1 + ref_files) + (0 if tested else 2) + min(3.0, churn.get(rel, 0) / 5) + min(2.0, size / 80)
            reasons = []
            if ref_files:
                reasons.append(f"{ref_files} referencing file(s)")
            if not tested:
                reasons.append("no test mentions it")
            if churn.get(rel, 0) >= 5:
                reasons.append(f"file changed {churn[rel]}x in 90d")
            if size >= 80:
                reasons.append(f"{size} lines")
            rows.append({"symbol": s["id"], "score": round(score, 1), "reasons": reasons})
    rows.sort(key=lambda r: (-r["score"], r["symbol"]))
    top = rows[0]["score"] if rows else 0.0
    verdict = "HIGH" if top >= 8 else "MEDIUM" if top >= 4.5 else "LOW"
    return {"base": base or "working tree vs HEAD", "verdict": verdict, "symbols": rows[:30], "changed_files": len(changed.get("files", []))}


def _layers(ctx: Any, args: dict[str, Any]) -> list[dict[str, Any]]:
    raw = args.get("layers")
    if not isinstance(raw, list):
        raw = project_config(ctx.root).get("layers") or []
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and item.get("name") and item.get("paths"):
            paths = item["paths"] if isinstance(item["paths"], list) else [item["paths"]]
            out.append({"name": str(item["name"]), "paths": [str(p) for p in paths]})
    return out


def get_layer_violations(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    from runtime import index_mcp

    layers = _layers(ctx, args)
    if len(layers) < 2:
        return {"error": "Declare at least two layers (top first): `layers: [{name, paths: [globs]}]` or [[layers]] in .terser/config.toml."}

    def layer_of(rel: str) -> int | None:
        for i, layer in enumerate(layers):
            if any(glob_match(rel, g) for g in layer["paths"]):
                return i
        return None

    violations = []
    for src, targets in sorted(index_mcp._file_graph(ctx).items()):
        ls = layer_of(src)
        if ls is None:
            continue
        for dst in sorted(targets):
            ld = layer_of(dst)
            if ld is not None and ld < ls:
                violations.append({"file": src, "layer": layers[ls]["name"], "imports": dst, "upper_layer": layers[ld]["name"]})
    return {"layers": [layer["name"] for layer in layers], "violations": violations[:200], "count": len(violations)}


def _workspace_globs(root: Path) -> list[tuple[str, str]]:
    import json

    from runtime import user_filters

    out: list[tuple[str, str]] = []
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            ws = data.get("workspaces")
            ws = ws.get("packages") if isinstance(ws, dict) else ws
            out.extend(("npm", str(g)) for g in ws or [] if isinstance(g, str))
        except (ValueError, OSError):
            pass
    pnpm = root / "pnpm-workspace.yaml"
    if pnpm.is_file():
        in_pk = False
        for line in pnpm.read_text(encoding="utf-8", errors="replace").splitlines():
            if re.match(r"^packages\s*:", line):
                in_pk = True
                continue
            if in_pk:
                m = re.match(r"^\s*-\s*['\"]?([^'\"#]+?)['\"]?\s*$", line)
                if m:
                    if not m.group(1).startswith("!"):
                        out.append(("pnpm", m.group(1)))
                elif line.strip() and not line.startswith(" "):
                    in_pk = False
    for name, keys, tool in (("Cargo.toml", ("workspace",), "cargo"), ("pyproject.toml", ("tool", "uv", "workspace"), "uv")):
        p = root / name
        if not p.is_file():
            continue
        try:
            node: Any = user_filters.parse_toml(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        for k in keys:
            node = node.get(k) if isinstance(node, dict) else None
        members = node.get("members") if isinstance(node, dict) else None
        out.extend((tool, str(g)) for g in members or [] if isinstance(g, str))
    gowork = root / "go.work"
    if gowork.is_file():
        text = gowork.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"^\s*use\s+(?:\(\s*([^)]*)\)|(\S+))", text, re.M):
            block = m.group(1) or m.group(2) or ""
            out.extend(("go", d.strip()) for d in block.split() if d.strip() and not d.startswith("//"))
    lerna = root / "lerna.json"
    if lerna.is_file() and not any(t == "npm" for t, _ in out):
        try:
            out.extend(("lerna", str(g)) for g in json.loads(lerna.read_text(encoding="utf-8")).get("packages") or [])
        except (ValueError, OSError):
            pass
    return out


def list_workspaces(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    root = ctx.root
    members: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool, pattern in _workspace_globs(root):
        pat = pattern.strip().rstrip("/").lstrip("./")
        try:
            dirs = [d for d in root.glob(pat) if d.is_dir()] if any(c in pat for c in "*?[") else [root / pat]
        except (ValueError, OSError):
            continue
        for d in dirs:
            try:
                rel = d.relative_to(root).as_posix()
            except ValueError:
                continue
            if rel in seen or not d.is_dir():
                continue
            seen.add(rel)
            nfiles = sum(1 for f in ctx.files if f.startswith(rel + "/"))
            nsyms = sum(1 for s in ctx.symbols if str(s.get("file", "")).startswith(rel + "/"))
            members.append({"path": rel, "tool": tool, "files": nfiles, "symbols": nsyms})
    members.sort(key=lambda m: m["path"])
    return {"workspaces": members, "hint": "Scope any action to one member with args.path_prefix=\"<path>\"."}


# ---------------------------------------------------------------------------
# SCIP import (minimal protobuf wire decoding)
# ---------------------------------------------------------------------------

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _fields(buf: bytes):
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, i = _varint(buf, i)
            yield field, wire, val
        elif wire == 2:
            ln, i = _varint(buf, i)
            yield field, wire, buf[i:i + ln]
            i += ln
        elif wire == 1:
            yield field, wire, buf[i:i + 8]
            i += 8
        elif wire == 5:
            yield field, wire, buf[i:i + 4]
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")


def _packed(buf: bytes) -> list[int]:
    out, i = [], 0
    while i < len(buf):
        v, i = _varint(buf, i)
        out.append(v)
    return out


def scip_symbol_name(symbol: str) -> str:
    """Simple name from a SCIP symbol string (`... pkg/Foo#bar().` -> `bar`)."""
    if symbol.startswith("local "):
        return ""
    s = re.sub(r"\([^)]*\)", "", symbol.rstrip(".#/:!"))
    parts = [p for p in re.split(r"[/#.:`\s]", s) if p]
    return parts[-1].strip("`") if parts else ""


def parse_scip(data: bytes) -> list[tuple[str, str, str, int, int]]:
    """(symbol, simple name, relative path, 1-based line, is_definition) occurrences."""
    rows = []
    for field, wire, val in _fields(data):
        if field != 2 or wire != 2:
            continue
        rel, occs = "", []
        for f2, w2, v2 in _fields(val):
            if f2 == 1 and w2 == 2:
                rel = v2.decode("utf-8", "replace")
            elif f2 == 2 and w2 == 2:
                occs.append(v2)
        for occ in occs:
            rng: list[int] = []
            sym, roles = "", 0
            for f3, w3, v3 in _fields(occ):
                if f3 == 1:
                    rng = _packed(v3) if w3 == 2 else rng + [v3]
                elif f3 == 2 and w3 == 2:
                    sym = v3.decode("utf-8", "replace")
                elif f3 == 3 and w3 == 0:
                    roles = v3
            name = scip_symbol_name(sym)
            if rel and sym and name and rng:
                rows.append((sym, name, rel, int(rng[0]) + 1, 1 if roles & 1 else 0))
    return rows


def _scip_db(ctx: Any) -> Path:
    from runtime import index_mcp

    return index_mcp._db_path(ctx.slug, ctx.env)


def import_scip(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(args.get("path") or "index.scip")).expanduser()
    if not path.is_absolute():
        path = ctx.root / path
    if not path.is_file():
        return {"error": f"SCIP file not found: {path} (generate one with scip-python, scip-typescript, rust-analyzer scip, ...)."}
    try:
        rows = parse_scip(path.read_bytes())
    except (ValueError, IndexError) as exc:
        return {"error": f"Could not decode SCIP index: {exc}"}
    dbp = _scip_db(ctx)
    try:
        with closing(sqlite3.connect(str(dbp), timeout=5.0)) as conn, conn:
            conn.execute("CREATE TABLE IF NOT EXISTS scip_occ (symbol TEXT, name TEXT, file TEXT, line INTEGER, is_def INTEGER)")
            conn.execute("CREATE INDEX IF NOT EXISTS scip_occ_name ON scip_occ(name)")
            conn.execute("DELETE FROM scip_occ")
            conn.executemany("INSERT INTO scip_occ VALUES (?, ?, ?, ?, ?)", rows)
    except sqlite3.Error as exc:
        return {"error": f"Could not store SCIP data: {exc}"}
    return {"imported": len(rows), "symbols": len({r[0] for r in rows}), "files": len({r[2] for r in rows}), "source": str(path)}


def scip_references(ctx: Any, name: str) -> list[dict[str, Any]] | None:
    """Precise references for ``name`` from an imported SCIP index, or None when absent."""
    dbp = _scip_db(ctx)
    if not dbp.is_file():
        return None
    try:
        with closing(sqlite3.connect(str(dbp), timeout=3.0)) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scip_occ'").fetchone():
                return None
            rows = conn.execute("SELECT file, line FROM scip_occ WHERE name = ? AND is_def = 0", (name,)).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    by_file: dict[str, list[int]] = {}
    for rel, line in rows:
        by_file.setdefault(str(rel), []).append(int(line))
    out = [{"file": f, "lines": sorted(set(ls))} for f, ls in by_file.items()]
    out.sort(key=lambda r: (-len(r["lines"]), r["file"]))
    return out


# ---------------------------------------------------------------------------
# Dispatch + formatting
# ---------------------------------------------------------------------------

_HANDLERS = {
    "get_hotspots": get_hotspots, "get_symbol_history": get_symbol_history,
    "get_untested_symbols": get_untested_symbols, "get_class_hierarchy": get_class_hierarchy,
    "find_implementations": find_implementations, "find_similar_symbols": find_similar_symbols,
    "get_symbol_diff": get_symbol_diff, "get_pr_risk_profile": get_pr_risk_profile,
    "get_layer_violations": get_layer_violations, "list_workspaces": list_workspaces,
    "import_scip": import_scip,
}


def dispatch(action: str, args: dict[str, Any], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    ctx = _ctx(args, env)
    if isinstance(ctx, dict):
        return ctx
    try:
        return _HANDLERS[action](ctx, args)
    except (ValueError, TypeError) as exc:
        return {"error": f"{action}: {exc}"}


def _tree(nodes: list[dict[str, Any]], indent: int = 1) -> list[str]:
    out = []
    for n in nodes:
        out.append(f"{'  ' * indent}{n['name']} ({n['file']})")
        out.extend(_tree(n.get("children") or [], indent + 1))
    return out


def format_response(action: str, p: dict[str, Any]) -> str | None:
    if "error" in p:
        return None
    if action == "get_hotspots":
        rows = [f"{r['score']:>7}  {r['file']} ({r['commits']} commits, {r['lines']} lines, {r['symbols']} symbols)" for r in p["hotspots"]]
        return f"Hotspots, last {p['days']} days (score = churn x size):\n" + "\n".join(rows or ["(none)"])
    if action == "get_symbol_history":
        return f"{p['symbol']} history:\n" + "\n".join(p["commits"] or ["(no commits found)"])
    if action == "get_untested_symbols":
        lines = [f"{p['untested_count']} of {p['checked']} symbols are never named in a test file:"]
        for r in p["untested"]:
            lines.append(f"{r['file']}: " + ", ".join(r["symbols"]))
        return "\n".join(lines)
    if action == "get_class_hierarchy":
        lines = [f"{p['symbol']} ({p['file'] or 'not indexed'})", "ancestors:"]
        lines += _tree(p["ancestors"]) or ["  (none)"]
        lines.append("descendants:")
        lines += _tree(p["descendants"]) or ["  (none)"]
        return "\n".join(lines)
    if action == "find_implementations":
        return f"Implementations of {p['symbol']} ({len(p['implementations'])}):\n" + "\n".join(p["implementations"] or ["(none)"])
    if action == "find_similar_symbols":
        rows = [f"{r['similarity']:.2f}  {r['a']}\n      {r['b']}" for r in p["pairs"]]
        return f"Similar symbols (Jaccard >= {p['threshold']}, {p['compared']} compared):\n" + "\n".join(rows or ["(none)"])
    if action == "get_symbol_diff":
        return f"{p['symbol']}: {p['status']}" + (f"\n{p['diff']}" if p.get("diff") else "")
    if action == "get_pr_risk_profile":
        lines = [f"Risk {p['verdict']} ({p['changed_files']} changed files, base: {p['base']}):"]
        for r in p["symbols"]:
            lines.append(f"  {r['score']:>4}  {r['symbol']}" + (f" - {'; '.join(r['reasons'])}" if r["reasons"] else ""))
        return "\n".join(lines)
    if action == "get_layer_violations":
        lines = [f"Layers (top first): {' > '.join(p['layers'])}; {p['count']} violation(s):"]
        lines += [f"  {v['file']} [{v['layer']}] imports {v['imports']} [{v['upper_layer']}]" for v in p["violations"]]
        return "\n".join(lines)
    if action == "list_workspaces":
        rows = [f"  {w['path']} ({w['tool']}, {w['files']} files, {w['symbols']} symbols)" for w in p["workspaces"]]
        return f"Workspace members ({len(rows)}):\n" + "\n".join(rows or ["  (none detected)"]) + f"\n{p['hint']}"
    if action == "import_scip":
        return f"Imported {p['imported']} SCIP occurrences ({p['symbols']} symbols, {p['files']} files) from {p['source']}; find_references now uses them."
    return None
