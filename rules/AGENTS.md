## Terser Policy

- **Symbol-first lookup**: On indexed repos, use `terser-index` (`order`/`route`: `search_symbols`, `get_symbol_source`, `get_file_outline`, `get_blast_radius`, `assemble_task_context`) before whole-file reads.
- **Targeted reads & CLI**: Use line ranges (`StartLine`/`EndLine` or `offset`/`limit`), batch parallel read-only calls, scope CLI output (`--stat`, `--max-count`), and query archived outputs via `terse query <id> "<terms>"`.
- **Pragmatic minimalism**: Check before adding code/deps: needed → already in repo → stdlib/native → existing dep → simpler code. Never cut validation, error handling, security, or accessibility. Keep prose clear and direct without filler or echoing unchanged code.
- **Stop on evidence**: Stop on `absent:*` or `_meta.budget_warning`. `PostToolUse` auto-reindexes edits.
