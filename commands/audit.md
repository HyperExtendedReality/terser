---
description: Run a full Terser context, config, and session waste audit for this project. Use when the user asks where tokens or context are being wasted.
---
Run `"${CLAUDE_PLUGIN_ROOT}/scripts/terse" --host claude audit` via Bash and summarize the top actionable fixes.

The audit ends with **regret signals**: quick byte-identical re-runs of a command within 2 minutes, and archive expands (`expand_sandbox` / `search_sandbox` / `terse expand|query`). Both suggest compression dropped something the agent needed. Call them out when they are non-zero, especially re-runs "after a Terser-compressed output".

If the user asks what Terser *would* save, or wants a counterfactual for sessions it did not touch, also run `"${CLAUDE_PLUGIN_ROOT}/scripts/terse" --host claude discover --replay --days 7`. It replays recorded transcripts through Terser's own compressors (shell output, file reads, MCP results, already via Terser), is read-only, and prints its accounting caveats; relay those caveats with the numbers.

Pass `--price-in <$/MTok>` and `--reread-turns <N>` (to `audit`, `discover` or `status`) when the user names their model price or wants a different value model; the printed assumptions line shows what was used.
