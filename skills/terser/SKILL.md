---
name: terser
description: Terser token status, savings audit, dashboard, FTS5 archive query/expand, repomap, and doctor CLI.
---

# Terser — Status, Audit & Dashboard

Run the Terser CLI (`terse` on macOS/Linux, `terse.cmd` on Windows, or `python <plugin>/runtime/cli.py`):

| Goal | Command |
| :--- | :--- |
| Unified status & live savings | `terse status` |
| Detailed token & cost audit | `terse audit` |
| Open/refresh local analytics dashboard | `terse dashboard` |
| Search an archived oversized output (SQLite FTS5) | `terse query <archive-id\|--last> "<keywords>"` |
| Expand a line range of an archived output | `terse expand <archive-id\|--last> [start end]` |
| Generate a token-budgeted PageRank repo map | `terse repomap [--budget 400]` |
| Verify hooks, MCP server & all 3 engines | `terse doctor` |

Show concise results directly. Never dump huge raw logs when `terse status` or `terse audit` summarizes them cleanly.
