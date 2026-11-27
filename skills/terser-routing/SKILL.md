---
name: terser-routing
description: Configure Terser's low-cost subagent model routing, Antigravity 2.0 model tier (flash_lite, flash, pro), reasoning effort (low, medium, high, max, inherit), and zero-token double-check verifier. Use when the user invokes /terser-routing or asks to route simple lookups to cheaper models.
---

# Terser Low-Cost Model Router & Double Check (`/terser-routing`)

Terser automatically routes routine, read-only lookup subagents (`research`, `explore`, or read-only `general-purpose` / `swarm` tasks) to a lower-cost model when the parent model didn't explicitly choose one — while keeping complex, write-intent, security, concurrency, or multi-part tasks on the parent model.

Every routed subagent is protected by a **zero-token double check**:
1. **Contract (`PreToolUse`)**: Appends `[Terser Verify]` requiring exact `path:line` citations, exact backticked identifiers, and `ESCALATE: <reason>` if the task exceeds simple lookups.
2. **Verifier (`PreInvocation` / `PostToolUse`)**: Deterministically checks cited files, line bounds, nearby symbols, hedging, and escalations at zero model-token cost. If anything fails, the parent model receives a concise `[Terser Verify]` alert to re-verify or re-run on its full model. Auto-backoff pauses routing for 7 days if failure rate exceeds 35% over >= 8 runs.

## Antigravity 2.0 Available Models & Efforts

- **Models (`subagent_cheap_model_antigravity`)**:
  - `flash_lite` — lightest, fastest (trivial / high-volume lookups)
  - `flash` — lighter, faster (**recommended default** for routine lookups)
  - `pro` — larger model for heavier subagent work
- **Efforts (`subagent_cheap_effort_antigravity`)**:
  - `low` (fast single-pass lookup), `medium` (cross-check callers/defs), `high` (thorough edge-case verification), `max` (exhaustive), or `inherit`

## How to Configure

1. **In the Antigravity 2.0 MCP Sidebar UI** (recommended):
   Call `terser_hud` (from `terser_terser-index`) or open **Terser HUD** from the side pane `+` menu or left sidebar to pick the model, reasoning effort, and double-check toggle live.
2. **Via CLI / `/terser-routing`**:
   Run `python runtime/cli.py routing <subcommand>` (or `terse routing <subcommand>`):
   - `terse routing status` — show active model, effort, double-check state, and 7-day pass/fail stats
   - `terse routing on` / `terse routing off` — enable or disable low-cost subagent routing
   - `terse routing model flash_lite` | `flash` | `pro` `--host antigravity` — choose Antigravity 2.0 subagent model
   - `terse routing effort low` | `medium` | `high` | `max` | `inherit` `--host antigravity` — choose effort level
   - `terse routing verify on` / `off` — toggle the zero-token double check
   - `terse routing check "<prompt>" research` — dry-run whether a prompt would route to the low-cost tier
