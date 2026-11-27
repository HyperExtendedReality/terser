"""Generate Python `difflib` reference cases for the native Rust T1 diff parity test.

Run: python scripts/gen_difflib_fixtures.py  (rewrites native/terser-fast/tests/fixtures/difflib_cases.json)
"""
from __future__ import annotations

import difflib
import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "native" / "terser-fast" / "tests" / "fixtures" / "difflib_cases.json"
VOCAB = ["", "x", "y", "}", "    return x", "def f():", "pass", "\t", "a = 1", "b = 2", "# c"]
ENDINGS = ["\n", "\n", "\n", "\r\n", "\r", " ", "\x0c"]


def _text(rng: random.Random, n: int) -> str:
    return "".join(rng.choice(VOCAB) + rng.choice(ENDINGS) for _ in range(n)) + rng.choice(["", "tail"])


def _mutate(rng: random.Random, text: str) -> str:
    lines = text.splitlines(keepends=True)
    for _ in range(rng.randint(0, 6)):
        op = rng.random()
        pos = rng.randint(0, len(lines))
        if op < 0.35 and lines:
            lines.pop(min(pos, len(lines) - 1))
        elif op < 0.7:
            lines.insert(pos, rng.choice(VOCAB) + "\n")
        elif lines:
            lines[min(pos, len(lines) - 1)] = rng.choice(VOCAB) + "!\n"
    return "".join(lines)


def main() -> None:
    rng = random.Random(1729)
    cases = []
    for _ in range(200):
        # Include >=200-line inputs so the autojunk "popular element" path is exercised.
        n = rng.choice([1, 3, 8, 25, 60, 90, 220])
        a = _text(rng, n)
        b = _mutate(rng, a)
        diff = "".join(difflib.unified_diff(
            a.splitlines(keepends=True), b.splitlines(keepends=True), fromfile="a/f.py", tofile="b/f.py", n=2,
        ))
        cases.append({"a": a, "b": b, "diff": diff})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(cases, ensure_ascii=True), encoding="utf-8")
    print(f"wrote {len(cases)} cases -> {OUT}")


if __name__ == "__main__":
    main()
