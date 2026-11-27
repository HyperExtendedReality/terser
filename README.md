<div align="center">

<picture>
  <img src="assets/banner.jpg" alt="Terser — Lossless Context Compression for AI Coding Agents" width="100%" />
</picture>

<br />

[![Version](https://img.shields.io/badge/version-1.2.0-0F766E?style=flat-square)](plugin.json)
[![Token Savings](https://img.shields.io/badge/measured%20savings-94.4%25-14B8A6?style=flat-square)](#measured-benchmarks)
[![Hook Latency](https://img.shields.io/badge/Rust%20hot--path-~10ms%20(14.1x)-F97316?style=flat-square)](#native-rust-hot-path-terser-fast)
[![Python](https://img.shields.io/badge/python-3.10%2B%20stdlib-3776AB?style=flat-square&logo=python&logoColor=white)](#requirements)
[![Hosts](https://img.shields.io/badge/hosts-Antigravity%20%7C%20Claude%20Code%20%7C%20Codex-6366F1?style=flat-square)](#quick-start)
[![License](https://img.shields.io/badge/license-GPL--3.0-0EA5E9?style=flat-square)](LICENSE)

**One fast, single-process, fail-open token-efficiency engine for Antigravity 2.0, Claude Code, and Codex.**

[Quick Start](#quick-start) · [Benchmarks](#measured-benchmarks) · [How It Works](#how-it-works) · [Rust Hot-Path](#native-rust-hot-path-terser-fast) · [CLI & Commands](#cli--slash-commands) · [Configuration](#configuration)

</div>

---

## Why Terser?

AI coding agents burn most of their context window on noise: bloated MCP tool schemas, repeated full-file reads, verbose build and test logs, repetitive JSON/NDJSON payloads, lockfile diffs, and retry loops.

**Terser** intercepts that waste across the entire tool and subagent lifecycle—**without sacrificing code quality, error fidelity, or KV prompt-cache hits**:

| Guarantee | What Terser Delivers |
| :--- | :--- |
| **94.4% Token Reduction** | Measured across 8 optimization layers on realistic developer workloads (`58,940` → `3,302` tokens). |
| **Zero Quality Compromise** | Errors, tracebacks, diffs, and exit codes are preserved **100% verbatim**. Full raw outputs are redacted and archived locally in SQLite FTS5 for instant drill-down. |
| **KV Prompt-Cache Safe** | Static-first, byte-stable session briefs and deterministic PageRank repo maps keep prompt prefix caches warm. |
| **~10 ms Native Hot-Path** | Default-on Rust accelerator (`terser-fast`) runs `PreToolUse` and `PostToolUse` hooks **14.1× faster** (~147 ms → ~10 ms), with lazy pure-Python stdlib fallback. |
| **Subagent-Native** | Full `SubagentStart` / `SubagentStop` lifecycle hooks, economy briefs, and complexity-aware subagent model routing across Antigravity 2.0, Claude Code, and Codex. |
| **100% Local & Fail-Open** | Zero runtime dependencies, zero network calls, zero telemetry. Every hook exits `0` on error so your workflow is never blocked. |

### What It Looks Like in Practice

```text
Without Terser                                    With Terser
───────────────────────────────────────────────   ──────────────────────────────────────────────────────
• MCP AST server loads 90+ schemas (~25,800 tok)  • 6-tool Counter front door (~1,050 tok, 95.9% cut)
• Re-reading unchanged 300L file (~3,603 tok)     • T0 hash dedup / edit-seeded hit (~38 tok, 98.9% cut)
• Re-reading after 2-line edit (~3,603 tok)       • T1 exact unified delta diff (~93 tok, 97.4% cut)
• Reading 300L code/docs/data file (~3.6k–5.7k)   • T2/T2b/T2c outline-first + range trim (~333–692 tok)
• Running cargo build / pytest (~447–505 tok)     • Stream filter + delta-diff (~34–94 tok, errors 100%)
• Oversized 50 KB log dumped into context         • Archived to SQLite FTS5 → terse query --last "ERR"
```

---

## Quick Start

### Requirements

- **Python 3.10+** — pure standard library; zero `pip install` or external MCP packages required (the `terser-index` SQLite-backed AST MCP server is built in).
- **Rust (`cargo`)** *(optional)* — prebuilt `terser-fast` binaries are fetched automatically from [GitHub Releases](https://github.com/HyperExtendedReality/terser/releases), or compiled locally if `cargo` is present.

### Install

```bash
git clone https://github.com/HyperExtendedReality/terser.git
cd terser
```

#### Antigravity 2.0 / IDE / `agy`

```powershell
python scripts/install.py --antigravity
```

Installs into `~/.gemini/config/plugins/terser/` with OS-native, zero-window-flash hooks and the 6-tool Counter MCP config (safe to re-run in place on Windows even while Antigravity is running).

> [!TIP]
> If you previously installed standalone token-optimization or code-indexing plugins, disable them in Antigravity Customizations to avoid duplicate hooks and schemas.

#### Claude Code

```bash
claude plugin marketplace add "/path/to/terser"
claude plugin install terser@terser

# Update after pulling new commits:
claude plugin update terser@terser
```

Or test for a single session without installing:

```bash
claude --plugin-dir "/path/to/terser"
```

#### Codex

```bash
python scripts/install.py --codex
codex plugin marketplace add "/path/to/terser"
codex plugin add terser@terser

# Update after pulling new commits:
codex plugin marketplace upgrade
```

#### Install Everywhere / Dry-Run / Uninstall

```bash
python scripts/install.py --all            # Configure Antigravity, Codex & print Claude commands
python scripts/install.py --all --dry-run  # Preview changes without modifying files
python scripts/install.py --all --uninstall
```

---

## Measured Benchmarks

Reproduce these numbers offline anytime with `scripts/terse benchmark` (or `python scripts/benchmark.py`):

| Optimization Layer | Before | After | Saved | Reduction |
| :--- | ---: | ---: | ---: | :--- |
| **MCP Schema** (6-tool Counter vs Full) | 25,800 tok | 1,050 tok | **95.9%** | `██████████` |
| **Read Funnel T0** (Unchanged Re-read Dedup) | 3,603 tok | 38 tok | **98.9%** | `██████████` |
| **Read Funnel T1** (Small-Edit Unified Delta) | 3,603 tok | 93 tok | **97.4%** | `██████████` |
| **Read Funnel T2** (Outline-First Code) | 3,603 tok | 692 tok | **80.8%** | `████████░░` |
| **Read Funnel T2b** (Outline-First Docs) | 5,753 tok | 333 tok | **94.2%** | `█████████░` |
| **Stream Output Filter** (Cargo Build) | 447 tok | 34 tok | **92.4%** | `█████████░` |
| **Lossless Columnar JSON Folding** | 1,751 tok | 244 tok | **86.1%** | `█████████░` |
| **Command Delta-Diff** (Repeat `pytest`) | 505 tok | 94 tok | **81.4%** | `████████░░` |
| **SQLite FTS5 Sandbox Query** | 5,678 tok | 462 tok | **91.9%** | `█████████░` |
| **SmartCrusher JSON** (120 API items) | 4,342 tok | 190 tok | **95.6%** | `██████████` |
| **Git Lockfile Diff Elision** | 3,855 tok | 72 tok | **98.1%** | `██████████` |
| **TOTAL (pooled across 8 layers)** | **58,940 tok** | **3,302 tok** | **94.4%** | `█████████▌` |

---

## How It Works

```mermaid
flowchart LR
    S["SessionStart / SubagentStart"] --> B["Static-First Brief & PageRank RepoMap"]
    P["PreToolUse (~10ms Rust)"] --> RF["4-Tier Read Funnel & MCP Dedup"]
    P --> FR["FormRepair, Chain Wrap & Index Grep"]
    O["PostToolUse"] --> OC["5-Stage Compression & Cmd Delta"]
    OC --> DB["SQLite FTS5 Sandbox (~/.terser/sandbox.db)"]
    O --> LG["Edit-Seed, Auto-Reindex & Loop Guard"]
```

### The 8-Layer Optimization Stack

1. **6-Tool SQLite-First AST Front Door (`terser-index`)**
   Exposes only 6 Counter tools (~1,050 schema tokens vs. ~25,800 full — **95.9% cut**) with a built-in SQLite-backed index (O(1) symbol & blast-radius lookup, auto-resolved `repo` from `cwd`, compact plain-text responses, and automatic Read Funnel cache seeding) while keeping all AST, graph, sandbox (`search_sandbox`, `expand_sandbox`), and filtered-exec (`execute_filtered`) actions one call away via `menu`, `order`, and `route`.
2. **4-Tier Read Funnel with Edit/Write Seeding**
   - **T0** unchanged re-read dedup · **T1** exact unified delta-diff on small edits · **T2/T2b/T2c** outline-first for large code, Markdown, and JSON/YAML/TOML/lockfile/log files (`>=10 KB`, `>=250L`) · **T3** instant passthrough for small/ranged reads with **disjoint interval tracking** and **partial-overlap trimming**.
   - **Edit & Write seeding** updates cached spans (including deterministic line-number shifting on insertions/deletions) right after edits so follow-up reads stay on T0/T1.
   - Built-in `soft_denies=1` retry escape, **auto-backoff** (`outline_backoff_limit=2`), and 120s read-only MCP refetch dedup prevent extra round-trips.
3. **FormRepair → 5-Stage Compression → Command Delta-Diff**
   Applies **FormRepair** and safe `&&`-chain wrapping first, then compresses both `stdout` and `stderr` via: ANSI/OSC-8 & spinner stripping → stream noise filters (Cargo, Go, pytest, pip/uv, pnpm/npm, Vite/tsc) + 25 output handlers → workspace-relative path elision, `grep`/`rg` per-file grouping, table-to-TSV compaction, lockfile diff elision, git diff header compaction, compiler-note dedup, framework stack-frame folding, identical traceback dedup, and base64 blob archival → duplicate-line collapse → cross-run **Command Delta-Diff** with volatile timing/timestamp normalization. Medium MCP payloads (`>1.5 KB`) use a strictly lossless-only path; tee-on-failure keeps 100% of errors, tracebacks, diffs, and exit codes.
4. **SmartCrusher JSON & NDJSON Pruner**
   Factors constant fields and columnizes repetitive JSON arrays, nested GraphQL payloads, and mixed NDJSON streams (preserving banners/headers and error/outlier items) while archiving the full raw payload for instant `terse query` retrieval.
5. **SQLite FTS5 Context Sandbox**
   Oversized command, MCP, `Glob`, and `Grep` outputs are redacted for secrets/PII, content-addressed, and chunk-indexed with overlapping windows + Porter stemming / BM25 ranking into `~/.terser/sandbox.db` (stdlib `sqlite3` FTS5, auto-pruned at 7 days / 50 MB). Rust `terser-fast exec` raw sidecars (`~/.terser/raw-archives/`) are lazy-ingested on demand so `terse query --last "<terms>"` and `terse expand --last 1 80` work seamlessly across both runtimes.
6. **Static-First Brief & Byte-Stable PageRank RepoMap**
   Emits a static-first, strictly budget-capped (`<=1600` chars) `SessionStart` brief plus an automatic `<150 ms` AST import-graph PageRank fallback (~300 tokens) *only* when a workspace is not yet indexed. Deterministic sorting and mtime fingerprinting keep KV prompt caches 100% warm.
7. **Subagent Lifecycle & Pragmatic Minimalism**
   Ships a `<220`-token `rules/AGENTS.md` policy, 3 focused skills (`terser`, `terser-index`, `terser-tune`), `SubagentStart` / `SubagentStop` hooks across Antigravity 2.0, Claude Code, and Codex, and optional `subagent_model_routing` for clearly low-complexity research subagents—explicitly forbidding cuts to validation, error handling, security, or accessibility.
8. **Unified Loop Guard & Smart Auto-Reindex**
   Per-session cooldown (45s), content-hashed edit signatures (no false positives on multi-region edits), and index-gated incremental AST re-indexing on file edits.

---

## Native Rust Hot-Path (`terser-fast`)

`scripts/install.py` enables the ~390 KB native Rust hot-path binary (`native/terser-fast/` → `bin/terser-fast`) **by default**—reusing an existing build, compiling with `cargo` if installed, or downloading the matching prebuilt binary from [GitHub Releases](https://github.com/HyperExtendedReality/terser/releases).

- **14.1× faster hooks:** Cuts `PreToolUse` latency from **~147 ms → ~10 ms**, with lazy Python discovery so shell launchers (`terse-hook` / `terse-hook.cmd` / `codex-hooks.json`) never pay Python lookup overhead on native fast paths.
- **Broad native parity:** Handles Read Funnel T0/T3, MCP refetch dedup, Loop Guard, `SubagentStart` / `SubagentStop`, task complexity routing, `PostToolUse` `terser-fast compress` (ANSI/OSC-8 strip, lockfile diff elision, git diff header compaction, table-to-TSV compaction, workspace path elision, compiler-note dedup, framework stack-frame folding, stream filters, duplicate-line collapse), and `terser-fast exec` (zero-overhead `run_command` wrapper with native command delta-diff, tee-on-failure, and raw archive sidecars).
- **Transparent Python fallback:** Cold-path and rich operations (AST outlines, unified diffs, FormRepair, SQLite FTS5 indexing/queries, audit/dashboard) delegate seamlessly to Python while sharing the exact same `~/.terser/` state files. Pass `--no-native` to run pure Python stdlib.

---

## CLI & Slash Commands

### Built-In Slash Commands & Skills

- **Claude Code slash commands:** `/terser:status`, `/terser:audit`, `/terser:dashboard`, `/terser:doctor`, `/terser:tune`
- **Agent skills (Antigravity, Claude Code & Codex):** `terser` (status/audit/dashboard), `terser-index` (AST symbol-first navigation), `terser-tune` (settings, micro-skills, tool pruning)

### `terse` CLI Quick Reference

Invoke via `scripts/terse` (macOS/Linux), `scripts/terse.cmd` (Windows), or `python runtime/cli.py`:

| Category | Command | Purpose |
| :--- | :--- | :--- |
| **Monitor** | `terse status` | Live status, per-layer % savings & outline override stats |
| | `terse benchmark` | Reproducible 8-layer local token-savings benchmark |
| | `terse audit` | Full session token & cost waste audit |
| | `terse audit-config` | Static context bloat & secret-leak audit |
| | `terse dashboard` | Refresh & open the local analytics dashboard |
| | `terse doctor` | Verify Python, Rust hot-path, engines, manifests & MCP |
| **Navigate** | `terse query <id\|--last> "<terms>"` | SQLite FTS5 search over an archived output (incl. Rust raw sidecars) |
| | `terse expand <id\|--last> [start end]` | Expand a line range (or full text) of an archive |
| | `terse repomap [--budget 400]` | Generate a byte-stable PageRank structural repo map |
| | `terse route "<task>"` | Zero-cost task complexity router (tier + score) |
| **Tune** | `terse config show` | Inspect active settings |
| | `terse config set <key>=<value>` | Toggle features (e.g. `shadow=true`, `outline_first=false`) |
| | `terse microskills [forget\|unforget]` | Manage learned repo EnvFacts micro-skills |
| | `terse toolprune [status\|apply\|restore]` | Review, apply, or restore unused tool pruning |
| | `terse coach` | Project-level token-efficiency coaching |

---

## Configuration

All local state and settings live under `~/.terser/` (override with `TERSER_HOME`). Persistent toggles can be set via `terse config set <key>=<value>` and reset anytime with `terse config reset`.

| Environment Variable | Effect |
| :--- | :--- |
| `TERSER_HOME` | Override state directory (default: `~/.terser`) |
| `TERSER_DISABLE=1` | Temporarily disable all hooks (instant passthrough) |
| `TERSER_SHADOW=1` | Shadow mode — log decisions without altering tool calls |
| `TERSER_PYTHON` | Explicit Python interpreter path for shell launchers & Rust delegation |
| `TERSER_OUTLINE_FIRST=0` | Disable outline-first reads for large files |
| `TERSER_BASH_COMPRESS=0` | Disable shell output compression |
| `TERSER_LOOP_GUARD=0` | Disable loop/thrash intervention |
| `TERSER_SUBAGENT_ROUTING=0` | Disable automatic complexity-based subagent model routing |
| `TERSER_AUTO_REINDEX=0` | Disable incremental AST auto-reindexing on edits |
| `TERSER_GREP_ANSWER=0` | Disable index-backed answers for huge Grep calls |

---

## Safety & Privacy

- **100% Local:** Zero outbound runtime network requests, zero telemetry, zero external API calls.
- **Fail-Open Architecture:** Every lifecycle hook is wrapped in defensive error handling and exits `0` so an unexpected edge case never blocks your agent.
- **Secret-Safe & Reversible:** Oversized outputs are redacted for secrets/PII before indexing and remain searchable in `~/.terser/sandbox.db` via `terse query --last` or `terse expand --last`. Shadow mode (`terse config set shadow=true`) lets you audit decisions with zero mutation.

---

<details>
<summary><strong>Development, Testing & Releases</strong></summary>

```bash
# Run the full Python & native integration test suite
python -m unittest discover -s tests -v

# Run Rust unit tests
cargo test --manifest-path native/terser-fast/Cargo.toml

# Run the local token-savings benchmark
python scripts/benchmark.py

# Force-rebuild the Rust hot-path accelerator (~10ms hook latency)
python scripts/install.py --build-native --antigravity

# Bump semantic version across all manifests & rebuild standalone dist/ bundles
python scripts/install.py --bump patch --build-dist
```

</details>

---

## License

[GNU General Public License v3.0 (GPL-3.0)](LICENSE) © 2026 HyperExtendedReality
