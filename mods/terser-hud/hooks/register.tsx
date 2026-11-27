import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register, RenderChildren } from 'claude-code'

import type { Cat, Hud } from '../types'

const PANE = 'terser-hud'
const DEFAULT_ROOT = 'C:/Dev/Game Dev/plugins/terser'

const hud = atom({ plugin: 'terser-hud', key: 'hud' } as const, null, { shape: 'hud-1' })
const fetchError = atom({ plugin: 'terser-hud', key: 'fetchError' } as const, null)
const isHidden = atom({ plugin: 'terser-hud', key: 'isHidden' } as const, false)

// ---------------------------------------------------------------- formatting

const tok = (n: number | undefined | null): string => {
  const v = n ?? 0
  if (v >= 1e9) return `${(v / 1e9).toFixed(2)}B`
  if (v >= 1e6) return `${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `${(v / 1e3).toFixed(1)}k`
  return String(v)
}
const usd = (n: number | undefined | null): string => {
  const v = n ?? 0
  return v >= 100 ? `$${v.toFixed(0)}` : v >= 1 ? `$${v.toFixed(2)}` : `$${v.toFixed(3)}`
}
const pct = (f: number | undefined | null): string => (f == null ? '–' : `${Math.round(f * 100)}%`)
const meter = (f: number, width = 10, on = '▰', off = '▱'): string => {
  const n = Math.max(0, Math.min(width, Math.round(f * width)))
  return on.repeat(n) + off.repeat(width - n)
}
const ctxColor = (f: number | null | undefined) =>
  f == null ? 'subtle' : f >= 0.85 ? 'error' : f >= 0.7 ? 'warning' : 'success'
const gradeColor = (letter: string | undefined) =>
  letter === 'A' ? 'success' : letter === 'B' ? 'suggestion' : letter === 'C' ? 'warning' : 'error'
const CAT_LABEL: Record<string, string> = {
  read_dedup: 'Read dedup',
  read_outline: 'Outline-first reads',
  read_delta: 'Read deltas',
  output_compress: 'Output compression',
  fts_sandbox: 'FTS sandbox',
  cmd_delta: 'Command deltas',
  form_repair: 'Form repair',
  mcp_refetch: 'MCP refetch guard',
}
const catLabel = (c: string) => CAT_LABEL[c] ?? c.replace(/_/g, ' ')
const FEATURE_LABEL: Record<string, string> = {
  read_cache: 'Read cache',
  delta_diff: 'Delta reads',
  outline_first: 'Outline first',
  form_repair: 'Form repair',
  bash_compress: 'Shell compress',
  cmd_delta: 'Cmd deltas',
  fts_sandbox: 'FTS sandbox',
  loop_guard: 'Loop guard',
  mcp_refetch_guard: 'MCP refetch',
  repo_map: 'Repo map',
  index_auto_reindex: 'Auto reindex',
  toolprune: 'Tool prune',
  code_discipline: 'Code discipline',
}

// ---------------------------------------------------------------- data feed

let refreshing: Promise<void> | null = null
let ticking = false

type CliResult = { ok: true; stdout: string } | { ok: false; error: string }

/** Summarise a Python failure: the last `File ..., line N` frame plus the error line. */
function pyError(stderr: string, code: number): string {
  const lines = stderr.trim().split(/\r?\n/).filter(l => l.trim())
  if (lines.length === 0) return `exit ${code}`
  const last = (lines[lines.length - 1] ?? '').trim()
  const frame = [...lines].reverse().find(l => /File ".*", line \d+/.test(l))
  const where = frame?.match(/File "(.*?)", line (\d+)/)
  const file = where ? `${(where[1] ?? '').split(/[\\/]/).pop()}:${where[2] ?? '?'}` : ''
  return file ? `${last} (${file})` : last
}

/** Runs `cli.py <args>` with the first Python that starts; reports the first real failure. */
async function runCli($: EngineInterface, cliArgs: string[], timeoutMs = 20000): Promise<CliResult> {
  const root = ((await $.env.get('TERSER_ROOT')) || DEFAULT_ROOT).replace(/\\/g, '/')
  const custom = await $.env.get('TERSER_PYTHON')
  let firstErr: string | null = null
  for (const py of custom ? [custom] : ['python', 'python3', 'py']) {
    try {
      const ran = await $.process.run([py, '-X', 'utf8', `${root}/runtime/cli.py`, ...cliArgs], {
        timeoutMs,
        env: { PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
      })
      if (ran.exitCode === 0) return { ok: true, stdout: ran.stdout }
      // The interpreter started and Terser itself failed: another Python would fail the same way.
      return { ok: false, error: `${py}: ${pyError(ran.stderr, ran.exitCode)}` }
    } catch (err) {
      firstErr ??= `${py}: ${err instanceof Error ? err.message : String(err)}`
    }
  }
  return { ok: false, error: firstErr ?? 'python not found (set TERSER_PYTHON)' }
}

async function refresh($: EngineInterface): Promise<void> {
  if (refreshing) return refreshing
  refreshing = (async () => {
    try {
      const { transcriptPath } = await $.session.info()
      const cwd = await $.session.cwd()
      const args = ['hud', '--json', '--cwd', cwd]
      if (transcriptPath) args.push('--transcript', transcriptPath)
      const ran = await runCli($, args)
      if (!ran.ok) {
        await update($, fetchError, () => ran.error.slice(0, 240))
        return
      }
      const line = ran.stdout.trim().split(/\r?\n/).pop() ?? ''
      let data: Hud
      try {
        data = JSON.parse(line) as Hud
      } catch {
        await update($, fetchError, () => `bad feed output: ${line.slice(0, 120)}`)
        return
      }
      if (data.error && !data.lifetime) {
        await update($, fetchError, () => (data.error ?? '').slice(0, 240))
        return
      }
      await update($, hud, () => data)
      await update($, fetchError, () => null)
    } catch (err) {
      await update($, fetchError, () => (err instanceof Error ? err.message : String(err)).slice(0, 240))
    } finally {
      refreshing = null
    }
  })()
  return refreshing
}

// ---------------------------------------------------------------- router

const MODEL_OPTIONS = [
  { value: 'off', label: 'Router off' },
  { value: 'haiku', label: 'Haiku' },
  { value: 'sonnet', label: 'Sonnet' },
  { value: 'fable', label: 'Fable' },
] as const
const EFFORT_OPTIONS = [
  { value: 'low', label: 'Low effort' },
  { value: 'medium', label: 'Medium effort' },
  { value: 'high', label: 'High effort' },
  { value: 'inherit', label: 'Inherit effort' },
] as const

const modelValue = (data: Hud | null): string => (!data?.routing?.enabled ? 'off' : data.routing.model || 'haiku')
const effortValue = (data: Hud | null): string => data?.routing?.effort || 'inherit'
const withCurrent = (opts: readonly { value: string; label: string }[], current: string) =>
  opts.some(o => o.value === current) ? [...opts] : [...opts, { value: current, label: current }]

async function runSteps($: EngineInterface, steps: string[][], ok: string): Promise<void> {
  for (const step of steps) {
    const ran = await runCli($, step)
    if (!ran.ok) {
      $.ui.toast(`Terser router: ${ran.error}`)
      await update($, fetchError, () => ran.error.slice(0, 240))
      return
    }
  }
  $.ui.toast(ok)
  await refresh($)
}

const setModel = ($: EngineInterface, value: string) =>
  value === 'off'
    ? runSteps($, [['routing', 'off']], 'Terser low-cost routing off')
    : runSteps($, [['routing', 'model', value], ['routing', 'on']], `Terser routes simple lookups to ${value}`)

const setEffort = ($: EngineInterface, value: string) =>
  runSteps($, [['routing', 'effort', value]], `Routed subagents: ${value} effort`)

const openPane = ($: EngineInterface) => $.ui.open({ id: PANE, title: 'Terser' })

// ---------------------------------------------------------------- hooks

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'terser-hud',
      description: 'Open the Terser details pane (savings, usage, cost, router, engines)',
    })
    void refresh($)
    // Keep the band current between turns (savings land from hooks outside turns too).
    if (!ticking) {
      ticking = true
      $.clock.every(90_000, () => void refresh($))
    }
    return next(e)
  })

  on('command.run', { command: 'terser-hud' }, async $ => {
    await update($, isHidden, () => false)
    await refresh($)
    await openPane($)
    return { text: 'Terser details opened.' }
  })

  on('turn.complete', async ($, e, next) => {
    const done = await next(e)
    void refresh($)
    return done
  })

  // ------------------------------------------------------------ band
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey || (await read($, isHidden))) return next(e)
    const data = await read($, hud)
    const err = await read($, fetchError)
    const { Box, Text, Button, Select } = $.ui.resolve(e)
    const s = data?.session ?? null

    const divider = <Text color="subtle">│</Text>
    const groups: RenderChildren[] = []

    if (!data) {
      groups.push(<Text color={err ? 'warning' : 'subtle'}>{err ? `⚠ ${err}` : 'Loading Terser stats…'}</Text>)
    } else {
      if (data.enabled === false || data.shadow) {
        groups.push(<Text color="warning" bold>{data.enabled === false ? 'DISABLED' : 'SHADOW'}</Text>)
      }
      // Savings: the headline number.
      groups.push(
        <Text>
          <Text color="success" bold>↓ {tok(s?.saved_tok ?? 0)}</Text>
          <Text color="subtle"> saved · </Text>
          <Text color="success">{usd(s?.saved_usd ?? 0)}</Text>
        </Text>,
      )
      // API usage and cost.
      if (s?.cost_usd != null) {
        const prompt = (s.input_tok ?? 0) + (s.cache_read_tok ?? 0) + (s.cache_write_tok ?? 0)
        groups.push(
          <Text>
            <Text bold>{usd(s.cost_usd)}</Text>
            <Text color="subtle"> · {tok(prompt)} in · {tok(s.output_tok)} out</Text>
          </Text>,
        )
      }
      // Context and cache health.
      if (s?.ctx_fill != null) {
        groups.push(
          <Text>
            <Text color="subtle">ctx </Text>
            <Text color={ctxColor(s.ctx_fill)}>{meter(s.ctx_fill, 8)} {pct(s.ctx_fill)}</Text>
            {s.cache_hit != null && <Text color="subtle"> · cache {pct(s.cache_hit)}</Text>}
          </Text>,
        )
      }
      if (s?.grade) {
        groups.push(
          <Text color={gradeColor(s.grade.letter)} bold inverse>
            {` ${s.grade.letter} `}
          </Text>,
        )
      }
      if (err) groups.push(<Text color="warning">⚠ stale</Text>)
    }

    return (
      <Box flexDirection="row" alignItems="center" columnGap={1} flexWrap="wrap" paddingX={1}>
        <Text color="claude" bold>◆ Terser</Text>
        {groups.flatMap(g => [divider, g])}
        <Box flexGrow={1} />
        {data && (
          <Box flexDirection="row" alignItems="center" columnGap={1}>
            <Text color="subtle">Router</Text>
            <Select key="router-model" options={withCurrent(MODEL_OPTIONS, modelValue(data))} value={modelValue(data)} onSelect={value => setModel($, value)} />
            {modelValue(data) !== 'off' && (
              <Select key="router-effort" options={withCurrent(EFFORT_OPTIONS, effortValue(data))} value={effortValue(data)} onSelect={value => setEffort($, value)} />
            )}
          </Box>
        )}
        <Button key="details" label="Details" variant="secondary" onPress={() => void openPane($)} />
        <Button key="hide" label="✕" plain dimColor onPress={() => update($, isHidden, () => true)} />
      </Box>
    )
  })

  // ------------------------------------------------------------ details pane
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Select } = $.ui.resolve(e)
    const data = await read($, hud)
    const err = await read($, fetchError)
    const s = data?.session ?? null
    const lt = data?.lifetime
    const cols = e.props.bodyColumns ?? 80
    const cardWidth = cols >= 100 ? Math.floor((cols - 3) / 2) : cols

    const card = (key: string, title: string, children: RenderChildren) => (
      <Box key={key} flexDirection="column" borderStyle="round" borderColor="subtle" paddingX={1} width={cardWidth} marginBottom={1}>
        <Text bold color="claude">{title}</Text>
        {children}
      </Box>
    )
    const row = (k: string, v: RenderChildren, color?: string) => (
      <Box flexDirection="row" justifyContent="space-between">
        <Text color="subtle">{k}</Text>
        <Text color={color} tabular>{v}</Text>
      </Box>
    )
    const tile = (label: string, value: string, sub: string, color?: string) => (
      <Box flexDirection="column" borderStyle="round" borderColor="subtle" paddingX={1} flexGrow={1} minWidth={16}>
        <Text color="subtle" size="caption">{label}</Text>
        <Text bold color={color} size="heading">{value}</Text>
        <Text color="subtle" size="caption">{sub}</Text>
      </Box>
    )
    const catRows = (cats: Cat[]) => {
      const max = Math.max(1, ...cats.map(c => c.saved_tok))
      return cats.map(c => (
        <Box flexDirection="column">
          <Box flexDirection="row" justifyContent="space-between">
            <Text>{catLabel(c.cat)}</Text>
            <Text tabular>
              <Text color="success" bold>{tok(c.saved_tok)}</Text>
              <Text color="subtle"> · {c.pct}% · {c.events} hits</Text>
            </Text>
          </Box>
          <Text color="success">{meter(c.saved_tok / max, Math.max(10, cardWidth - 6), '━', '─')}</Text>
        </Box>
      ))
    }

    const header = (
      <Box flexDirection="row" alignItems="center" columnGap={1} marginBottom={1}>
        <Text bold color="claude">◆ Terser</Text>
        <Text color="subtle">{data?.version ? `v${data.version}` : ''}</Text>
        <Text color={data?.enabled === false || data?.shadow ? 'warning' : 'success'}>
          {data ? (data.enabled === false ? '● disabled' : data.shadow ? '● shadow mode' : '● active') : '…'}
        </Text>
        <Box flexGrow={1} />
        <Button key="refresh" label="Refresh" variant="secondary" onPress={() => refresh($)} />
        <Button key="band" label="Show band" plain onPress={() => update($, isHidden, () => false)} />
      </Box>
    )

    if (!data) {
      return (
        <Box flexDirection="column">
          {header}
          <Text color={err ? 'warning' : 'subtle'}>{err ? `⚠ Could not read Terser: ${err}` : 'Loading Terser stats…'}</Text>
        </Box>
      )
    }

    const window = s?.ctx_window ?? 200000
    const promptTok = (s?.input_tok ?? 0) + (s?.cache_read_tok ?? 0) + (s?.cache_write_tok ?? 0)
    const pr = data.price
    const features = Object.entries(data.features ?? {})
    const r = data.routing

    return (
      <Box flexDirection="column">
        {header}
        {err && <Text color="warning">⚠ Last refresh failed: {err}</Text>}

        {/* KPI tiles */}
        <Box flexDirection="row" flexWrap="wrap" columnGap={1} marginBottom={1}>
          {tile('Saved this session', tok(s?.saved_tok), `≈ ${usd(s?.saved_usd)} · ${s?.events ?? 0} events`, 'success')}
          {tile('Est. API cost', s?.cost_usd != null ? usd(s.cost_usd) : '–', `${s?.turns ?? 0} turns · ${s?.model || 'model ?'}`)}
          {tile('Context', pct(s?.ctx_fill), `of ${tok(window)} · peak ${tok(s?.peak_ctx)}`, ctxColor(s?.ctx_fill))}
          {tile('Quality', s?.grade ? s.grade.letter : '–', s?.grade ? `${s.grade.score}/100` : 'no transcript', gradeColor(s?.grade?.letter))}
        </Box>

        <Box flexDirection="row" flexWrap="wrap" columnGap={1}>
          {s?.cost_usd != null &&
            card('usage', 'API usage', (
              <Box flexDirection="column">
                {row('Uncached input', tok(s.input_tok))}
                {row('Cache read', tok(s.cache_read_tok))}
                {row('Cache write', tok(s.cache_write_tok))}
                {row('Output', tok(s.output_tok))}
                {row('Prompt total', tok(promptTok))}
                {row('Cache hit rate', pct(s.cache_hit), s.cache_hit != null && s.cache_hit >= 0.8 ? 'success' : undefined)}
                <Text color={ctxColor(s.ctx_fill)}>{meter(s.ctx_fill ?? 0, Math.max(10, cardWidth - 4))}</Text>
              </Box>
            ))}

          {card('session-savings', 'Savings · this session', (
            <Box flexDirection="column">
              {row('Kept out of context', `${tok(s?.saved_tok)} · ≈ ${usd(s?.saved_usd)}`, 'success')}
              {s?.by_cat && s.by_cat.length > 0 ? catRows(s.by_cat) : <Text color="subtle">No savings yet this session.</Text>}
            </Box>
          ))}

          {s?.grade &&
            card('quality', 'Session quality', (
              <Box flexDirection="column">
                {row('Grade', `${s.grade.letter} · ${s.grade.score}/100`, gradeColor(s.grade.letter))}
                {s.grade.reasons.length === 0 ? (
                  <Text color="success">No avoidable rework detected.</Text>
                ) : (
                  s.grade.reasons.map(reason => <Text color="warning">• {reason}</Text>)
                )}
                {row('Tool calls', `${s.tool_calls ?? 0} · ${s.tool_errors ?? 0} failed`)}
                {row('Repeated reads', String(s.repeated_reads ?? 0))}
                {row('Quick re-runs', String(s.quick_reruns ?? 0))}
                {row('Archive expands', String(s.archive_expands ?? 0))}
                {row('Oversized results', String(s.big_results ?? 0))}
                {s.top_tools && s.top_tools.length > 0 && (
                  <Text color="subtle" wrap="wrap">Top: {s.top_tools.slice(0, 6).map(t => `${t.tool} ${t.n}`).join(' · ')}</Text>
                )}
              </Box>
            ))}

          {r &&
            card('router', 'Low-cost subagent router', (
              <Box flexDirection="column" rowGap={1}>
                <Box flexDirection="row" columnGap={1} flexWrap="wrap">
                  <Select key="pane-router-model" label="Model" options={withCurrent(MODEL_OPTIONS, modelValue(data))} value={modelValue(data)} onSelect={value => setModel($, value)} />
                  <Select key="pane-router-effort" label="Effort" options={withCurrent(EFFORT_OPTIONS, effortValue(data))} value={effortValue(data)} onSelect={value => setEffort($, value)} />
                </Box>
                <Box flexDirection="column">
                  {row('Double check', r.verify ? 'on' : 'off')}
                  {row('Last 7 days', `${r.routed ?? 0} routed · ${r.checked ?? 0} checked · ${r.failed ?? 0} failed`)}
                  {r.backed_off && <Text color="warning">Auto backoff active: routing paused after failed checks.</Text>}
                  <Text color="subtle" wrap="wrap">
                    Only read-only lookups (Explore / research) that set no model are routed; never security, migrations or long briefs.
                  </Text>
                </Box>
              </Box>
            ))}

          {card('lifetime', 'Savings · all time', (
            <Box flexDirection="column">
              {row('Kept out of context', `${tok(lt?.saved_tok)} · ≈ ${usd(lt?.saved_usd)}`, 'success')}
              {row('Terser events', String(lt?.events ?? 0))}
              {lt?.by_cat && lt.by_cat.length > 0 && catRows(lt.by_cat)}
            </Box>
          ))}

          {(data.index || (data.funnel && data.funnel.length > 0)) &&
            card('index', 'Code index & read funnel', (
              <Box flexDirection="column">
                {data.index && row('Workspace', `${data.index.repo}`)}
                {data.index && row('Indexed', `${data.index.files} files · ${data.index.symbols} symbols`)}
                {(data.funnel ?? []).map(f =>
                  row(`Funnel ${f.tier}`, `${f.served ?? 0} served · ${f.retry ?? 0} retried${f.mismatch ? ` · ${f.mismatch} caught` : ''}`),
                )}
              </Box>
            ))}

          {features.length > 0 &&
            card('engines', 'Engines', (
              <Box flexDirection="row" flexWrap="wrap" columnGap={2}>
                {features.map(([k, v]) => (
                  <Text color={v === false ? 'subtle' : 'text'}>
                    <Text color={v === false ? 'subtle' : 'success'}>{v === false ? '○' : '●'}</Text> {FEATURE_LABEL[k] ?? k}
                    {typeof v === 'string' ? <Text color="subtle"> {v}</Text> : ''}
                  </Text>
                ))}
              </Box>
            ))}

          {pr &&
            card('price', 'Value model', (
              <Box flexDirection="column">
                {row('Input / output', `$${pr.input_per_mtok} / $${pr.output_per_mtok} per MTok`)}
                {row('Cache write / read', `×${pr.cache_write_mult} / ×${pr.cache_read_mult}`)}
                <Text color="subtle" wrap="wrap">
                  A kept-out token counts as 1 cache write + {pr.reread_turns} re-reads. Set price_input_per_mtok and price_output_per_mtok in Terser config to match your model.
                </Text>
              </Box>
            ))}
        </Box>
      </Box>
    )
  })
}
