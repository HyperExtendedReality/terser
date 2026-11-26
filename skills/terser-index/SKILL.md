---
name: terser-index
description: Token-efficient AST code navigation, symbol retrieval, call hierarchy, blast radius, and safe refactoring through Terser's 6-tool Counter front door.
---

# Terser Index — Symbol-First Navigation Workflow

`terser-index` defaults to the **6-tool Counter front door** (~1,050 schema tokens vs ~25,800 full), keeping all 90+ AST/graph actions one call away:

1. **Discover / route**: call `route(task="...", repo="...")` (or `menu(query="...")` for exact arg shapes).
2. **Locate symbols**: `order(action="search_symbols", args={"repo": ..., "query": ...})` → then fetch ONLY the exact body with `order(action="get_symbol_source", args={"repo": ..., "symbol_id": ...})`.
3. **One-call task context**: `order(action="assemble_task_context", args={"repo": ..., "task": ..., "token_budget": 4000})`.
4. **Structural & preflight safety**: `get_file_outline`, `find_importers`, `check_references`, `get_call_hierarchy`, `get_blast_radius`, `check_edit_safe`, `check_delete_safe`, `find_dead_code`.
5. **Index a new folder**: `order(action="index_folder", args={"path": "<abs-path>"}, allow_state_change=true)`.

Rules:
- Stop immediately on citable absence (`_meta.verdict.evidence_ref` starts with `absent:`) or `stop_rule.terminal == true`.
- On small files (< 250 lines) or when you already know exact line numbers, use targeted `read_file` / `Read` with line ranges directly.
- Edits trigger automatic background re-indexing via `PostToolUse`.
