#!/usr/bin/env python3
"""Bundle the pure-stdlib engines from TokenEconomizer and Token Optimizer into vendor/.

Run whenever the sibling source checkouts are updated:
    python scripts/bundle_vendors.py
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
    te_src = WORKSPACE / "TokenEconomizer" / "tokeneconomizer"
    to_src = WORKSPACE / "token-optimizer" / "skills" / "token-optimizer" / "scripts"
    to_hooks_src = WORKSPACE / "token-optimizer" / "hooks"

    if not te_src.is_dir():
        print(f"ERROR: TokenEconomizer source not found at {te_src}", file=sys.stderr)
        return 1
    if not to_src.is_dir():
        print(f"ERROR: Token Optimizer scripts not found at {to_src}", file=sys.stderr)
        return 1

    VENDOR.mkdir(parents=True, exist_ok=True)

    # 1. Vendor TokenEconomizer core (pure Python stdlib)
    te_dst = VENDOR / "tokeneconomizer"
    if te_dst.exists():
        shutil.rmtree(te_dst)
    shutil.copytree(te_src, te_dst, ignore=IGNORE)

    # 2. Vendor Token Optimizer scripts + _measure + detectors (pure Python stdlib)
    to_dst = VENDOR / "token_optimizer"
    if to_dst.exists():
        shutil.rmtree(to_dst)
    shutil.copytree(to_src, to_dst, ignore=IGNORE)

    # 3. Copy Token Optimizer consolidated hook runners for Claude/Codex deep integration
    if to_hooks_src.is_dir():
        hooks_dst = VENDOR / "token_optimizer_hooks"
        if hooks_dst.exists():
            shutil.rmtree(hooks_dst)
        shutil.copytree(to_hooks_src, hooks_dst, ignore=IGNORE)

    te_count = sum(1 for _ in te_dst.rglob("*.py"))
    to_count = sum(1 for _ in to_dst.rglob("*.py"))
    print(f"Bundled vendor/tokeneconomizer ({te_count} .py files)")
    print(f"Bundled vendor/token_optimizer ({to_count} .py files)")
    return 0


if __name__ == "__main__":
    sys.exit(bundle())
