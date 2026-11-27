#!/usr/bin/env python3
"""Bundle the pure-stdlib TokenEconomizer engine (MIT, Microsoft) into vendor/.

Run whenever the sibling source checkout is updated:
    python scripts/bundle_vendors.py

The upstream LICENSE is copied with the code; MIT requires that notice to ship.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = ROOT.parent
VENDOR = ROOT / "vendor"

IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", ".pytest_cache", ".DS_Store", "*.egg-info",
)


def bundle() -> int:
    te_root = WORKSPACE / "TokenEconomizer"
    te_src = te_root / "tokeneconomizer"
    if not te_src.is_dir():
        print(f"ERROR: TokenEconomizer source not found at {te_src}", file=sys.stderr)
        return 1
    VENDOR.mkdir(parents=True, exist_ok=True)
    te_dst = VENDOR / "tokeneconomizer"
    if te_dst.exists():
        shutil.rmtree(te_dst)
    shutil.copytree(te_src, te_dst, ignore=IGNORE)
    lic = te_root / "LICENSE"
    if lic.is_file():
        shutil.copy2(lic, te_dst / "LICENSE")
    te_count = sum(1 for _ in te_dst.rglob("*.py"))
    print(f"Bundled vendor/tokeneconomizer ({te_count} .py files)")
    return 0


if __name__ == "__main__":
    sys.exit(bundle())
