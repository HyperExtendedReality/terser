---
name: terser-tune
description: Configure Terser settings, inspect or forget learned repository micro-skills (EnvFacts), review/apply/restore unused tool pruning, and get token-efficiency coaching.
---

# Terser Tune — Micro-Skills, Tool Pruning & Configuration

Run via the Terser CLI (`terse` / `terse.cmd` / `python <plugin>/runtime/cli.py`):

| Goal | Command |
| :--- | :--- |
| Show active settings | `terse config show` |
| Toggle a feature (`outline_first`, `bash_compress`, `form_repair`, `shadow`, etc.) | `terse config set <key>=<value>` |
| Switch Terser AST MCP surface (`counter` \| `core` \| `full`) | `terse config set index_surface=counter` |
| View/forget/unforget learned repo micro-skills | `terse microskills [forget <id> \| unforget <id>]` |
| Review/apply/restore unused tool pruning (Claude/Codex) | `terse toolprune [status \| apply \| restore]` |
| Run project token-efficiency coach | `terse coach` |

Every optimization is reversible and fail-open. Setting `shadow=true` logs decisions without altering tool calls.

## Pragmatic Bloat Review

When reviewing a diff or auditing a module for unnecessary code/token bloat, check in order:
1. Does this code or abstraction need to exist at all?
2. Is equivalent functionality already in the repository?
3. Does the language standard library or native platform feature handle it?
4. Does an already-installed dependency cover it?
5. Can it be written more simply without losing clarity?
6. **Non-negotiable:** Never remove input validation, error handling, security checks, or accessibility.
