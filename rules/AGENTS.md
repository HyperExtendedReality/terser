## Terser Policy

- **Symbol-first lookup**: On indexed repos, use `terser-index` (`order`/`route`: `search_symbols`, `get_symbol_source`, `get_file_outline`, `get_blast_radius`, `assemble_task_context`) before whole-file reads.
- **Targeted reads & CLI**: Use line ranges (`StartLine`/`EndLine` or `offset`/`limit`), batch parallel reads, scope CLI output (`--stat`, `--max-count`), and query archives via `terse query <id> "<terms>"`.
- **Pragmatic minimalism**: Needed → in repo → stdlib → existing dep → simpler code. Never cut validation, error handling, security, or accessibility. Be concise; don't echo unchanged code.
- **Subagents & evidence**: Stop on `absent:*` or `_meta.budget_warning` (`PostToolUse` auto-reindexes). Scope subagent prompts and require path+line summaries; subagents follow this policy.
