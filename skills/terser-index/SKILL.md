---
name: terser-index
description: Symbol-first AST navigation, source retrieval, blast radius, and safe refactoring via Terser's 6-tool Counter MCP. Use before Grep/Read whenever you need to find where a function, class, or symbol is defined or used, who calls it, what an edit or delete would break, or the minimal source for a task in an indexed repo (the session brief says "Code index ... ready").
---

# Terser Index — Symbol-First Navigation Workflow

`terser-index` exposes a **6-tool Counter front door** (`menu`, `route`, `order`, `index_folder`, `search_symbols`, `get_symbol_source`). Every other action below runs through `order(action="<name>", args={...})`. `repo` is optional when the cwd (or the only indexed repo) resolves it.

1. **Discover / route**: `route(task="...")` recommends an action; `menu(query="...")` prints exact arg shapes.
2. **Locate symbols**: `search_symbols` → then fetch ONLY the exact body with `get_symbol_source` (`symbol_id`, optional `start_line`/`end_line`). Batch several with `get_symbols` (`symbol_ids`).
3. **One-call task context**: `assemble_task_context` (`task`, `token_budget`).
4. **Structure**: `get_repo_outline`, `get_file_tree`, `get_file_outline` (`file_path`), `search_text` (`query`).
5. **Usage & graph**:
   - `find_references` (`symbol`): every line using the name, definition lines excluded.
   - `find_importers` (`module`: dotted name or repo-relative path): files importing it, tagged `[relative]`/`[name]` when the match is weaker than exact.
   - `get_call_hierarchy` (`symbol`, `depth` 1-4, default 2): callers and callees tree.
   - `get_blast_radius` (`symbol`): files referencing a symbol, with def/importer tags.
   - `get_dependency_cycles`: import cycles between indexed files.
6. **Preflight safety** (verdict + affected files):
   - `check_edit_safe` (`symbol`): SAFE / CAUTION / RISKY.
   - `check_delete_safe` (`symbol`): SAFE / UNSAFE.
   - `check_rename_safe` (`symbol`, `new_name`): SAFE / REVIEW / CONFLICT.
   - UNKNOWN means the name is not an indexed symbol.
7. **Review**: `get_changed_symbols` (`ref` default worktree vs index, `staged`, `include_untracked`) maps `git diff` hunks to symbols; `find_dead_code` (`limit`, `include_tests`) lists symbols whose name is never referenced. Both are name-based: dynamic dispatch and reflection are not seen.
8. **Archived output** (from compressed tool results): `search_sandbox` (`archive_id` or `--last`, `query`) runs Porter + trigram search merged by reciprocal rank fusion, and retries once with a spelling correction when nothing matches; `expand_sandbox` reads a line range (omit lines for the full text); `index_sandbox_file` (`path`) indexes a large doc or log; `purge_sandbox` deletes by `archive_id`, `session`, or `all: true`.
9. **Commands**: `execute_filtered` (`argv`, `intent`, `cwd`) runs a safe command and returns only the matching parts of large output. `execute_batch` (`commands` list, `queries`, `cwd`) runs up to 8 commands in one call the same way.
10. **Index maintenance**: git repos are indexed automatically in the background at session start (up to `index_auto_init_max_files`). For other folders, or to force a refresh: `order(action="index_folder", args={"path": "<abs-path>"})`. `list_repos` lists indexes; `invalidate_cache` (`repo`) deletes one.
11. **Code Mode** (the work runs locally; only the result comes back):
    - `run_script` (`code`, `language` python|node|bash|powershell, `intent`, `apply`): write a short script instead of reading files. Python scripts get `t`: `t.files(glob)`, `t.grep(rx, glob)`, `t.count(rx, glob)`, `t.symbols(q)`, `t.source(id)`, `t.refs(name)`, `t.read(p, start, end)`, `t.json/toml/csv/ini/py_ast(p)`, and staged edits `t.replace(p, old, new)`, `t.sub(p, rx, repl)`, `t.write(p, text)`, `t.edit_symbol(id, src)`. Print only what you need. Edits come back as a diff unless `apply: true`; a script that fails writes nothing.
    - `code_search` (`all`, `any`, `none` regex lists, `glob`, `mode` files|count|hits): "files that use X but not Y" in one call.
    - `code_edit` (`edits`: `{path, old, new}`, `{path|glob, pattern, repl}`, `{symbol_id, source}`, `{path, content}`; `apply`): many edits in one call, checked together, written all-or-nothing.
    - `code_query` (`path`, `select` dotted path like `dependencies.*` or `items[0].name`, or `outline: true`): values from JSON/JSONL/TOML/CSV/INI without reading the file.
    - `fetch_and_index` (`url`, `query`): fetch a page once, archive it, get its outline plus matching sections; follow up with `search_sandbox`.
12. **Analysis** (all accept `path_prefix` to scope a monorepo member):
    - `get_hotspots` (`days`): files ranked by churn x size. `get_symbol_history` (`symbol`): commits touching one symbol.
    - `get_untested_symbols`: functions/classes no test file names. `find_similar_symbols` (`threshold`, `symbol`, `structural`): near-duplicates.
    - `get_class_hierarchy` / `find_implementations` (`symbol`): ancestors, descendants, implementers.
    - `get_symbol_diff` (`symbol`, `ref`): one symbol's change. `get_pr_risk_profile` (`base`): changed symbols scored by refs, tests, churn, size.
    - `get_layer_violations` (`layers` or `[[layers]]` in `.terser/config.toml`), `list_workspaces`, `import_scip` (`path`: a `.scip` file, after which `find_references` is compiler-precise).

Rules:
- On small files (< 250 lines) or when you already know exact line numbers, use targeted `read_file` / `Read` with line ranges directly.
- Prefer Code Mode when the answer needs many files read but only a few lines reported (counting, cross-file checks, config lookups, codemods).
- Edits trigger automatic background re-indexing via `PostToolUse`; Code Mode writes refresh the index and read cache themselves.
- Tool search: the six front-door schemas are ~1k tokens, so they rarely need deferring. If your client defers MCP schemas (Anthropic tool search), keep `order` and `route` loaded and let `menu` describe the rest on demand.
