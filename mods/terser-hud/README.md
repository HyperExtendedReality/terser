# terser-hud

A Claude Code mod that shows a live Terser status band above the prompt, plus a details pane.

**Band** (refreshes after every turn): tokens Terser kept out of context this session and their value,
estimated API cost, prompt/output tokens, context fill (green → amber at 70% → red at 85%),
cache hit rate, session quality grade, and all-time savings. The **Router** dropdown picks the low-cost model
Terser routes simple read-only lookup subagents to (Off / Haiku / Sonnet / Fable; runs `terse routing`). **Details** opens the pane; **Hide** hides the band
(`/terser-hud` brings it back).

**Pane**: per-engine savings bars (session and all time), full API usage breakdown (uncached input, cache read,
cache write, output, model, turns), context bar with peak, quality grade with its reasons and regret signals,
top tools, code-index and read-funnel stats, which Terser engines are on, and the price model behind the cost figures.

Data comes from `terse hud --json` (`runtime/hud.py` in the Terser plugin). The mod finds Terser at
`C:/Dev/Game Dev/plugins/terser`; set `TERSER_ROOT` to point elsewhere and `TERSER_PYTHON` to pick the interpreter.
Costs use Terser's configured value model ($3/$15 per MTok by default), so set `price_input_per_mtok` and
`price_output_per_mtok` in Terser's config to match the model you run.
