---
name: terser
description: Terser token savings, status, audit, dashboard, health check, and archived-output retrieval. Use when the user asks how many tokens Terser saved, whether Terser is working or broken, for a token/context waste audit or dashboard, or when a tool result says output was omitted, truncated, or "full output archived/indexed" and you need the rest of it.
---

# Terser — Status, Audit, Dashboard & Archive Retrieval

## Retrieve archived output (no shell needed)

When a result says lines were omitted or the full output was archived, fetch exactly what you need through the Terser index MCP instead of re-running the command:

- Search: `order(action="search_sandbox", args={"archive_id": "<id or --last>", "query": "<terms>"})`
- Line range: `order(action="expand_sandbox", args={"archive_id": "<id or --last>", "start_line": 1, "end_line": 100})`
- Full text: `order(action="expand_sandbox", args={"archive_id": "<id>"})`

## CLI

The launcher is `scripts/terse` (POSIX shell, incl. Git Bash) or `scripts\terse.cmd` (PowerShell/cmd; in PowerShell prefix the quoted path with `&`), two directories above this skill's base directory. It is usually **not** on `PATH`, so call it by its quoted absolute path, e.g. `"<plugin>/scripts/terse" --host claude status`.

| Goal | Subcommand |
| :--- | :--- |
| Unified status & live savings | `status` |
| Detailed token & cost audit | `audit` |
| Open/refresh local analytics dashboard | `dashboard` |
| Verify hooks, MCP server & all 3 engines | `doctor` |
| Search an archived output (SQLite FTS5) | `query <archive-id\|--last> "<keywords>"` |
| Expand a line range of an archived output | `expand <archive-id\|--last> [start end]` |
| Generate a token-budgeted PageRank repo map | `repomap [--budget 400]` |

Show concise results directly. Never dump huge raw logs when `status` or `audit` summarizes them cleanly.
