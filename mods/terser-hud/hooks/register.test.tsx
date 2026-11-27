import { expect, test } from 'claude-code/testing'

const FEED = {
  version: '1.2.0',
  enabled: true,
  shadow: false,
  lifetime: { saved_tok: 1681379, saved_usd: 7.82, events: 310, by_cat: [{ cat: 'read_dedup', saved_tok: 1295466, events: 61, pct: 99.8 }] },
  session: {
    saved_tok: 48200, saved_usd: 0.22, events: 12, by_cat: [],
    model: 'claude-opus-5-5', turns: 28, input_tok: 56, output_tok: 25088,
    cache_read_tok: 2796367, cache_write_tok: 204265, cost_usd: 1.98,
    ctx_fill: 0.61, ctx_window: 200000, peak_ctx: 122809, cache_hit: 0.93,
    tool_calls: 40, tool_errors: 1, grade: { letter: 'A', score: 100, reasons: [] },
  },
  features: { read_cache: true, loop_guard: false },
  routing: { enabled: false, model: 'haiku', verify: true, routed: 0, checked: 0, failed: 0, backed_off: false },
}

const BAND = { plugin: 'terser-hud', component: 'AbovePrompt', props: {
    hasSurvey: false, isCollapsed: false, isWorking: false, maxRows: 4, bodyColumns: 120,
    scroll: { offset: 0, bodyRows: 4 }, view: {},
  },
} as const
const PANE = {
  plugin: 'terser-hud', component: 'Pane', requestId: 'terser-hud',
  props: { title: 'Terser details', isFocused: false, bodyColumns: 80, placement: 'dock', scroll: { offset: 0, bodyRows: 40 }, view: {} },
} as const

async function setup(
  $: any,
  on: any,
  argvs: string[][] = [],
  result: { exitCode: number; stdout: string; stderr: string } = { exitCode: 0, stdout: JSON.stringify(FEED), stderr: '' },
) {
  on('clock.every', () => ({ value: null }))
  on('ui.toast', () => ({ value: null }))
  on('command.register', () => ({ value: { command: 'terser-hud' } }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('env.get', () => ({ value: undefined }))
  on('session.info', () => ({ value: { transcriptPath: 'C:/t/abc.jsonl' } }))
  on('session.cwd', () => ({ value: 'C:/repo' }))
  on('process.run', (_: unknown, e: { argv: readonly string[] }) => {
    argvs.push([...e.argv])
    return { value: { isStdoutTruncated: false, isStderrTruncated: false, ...result } }
  })
  on('session.start', (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('command.run', () => ({ text: '' }))
  await $.session.start({ cwd: 'C:/repo', surface: 'desktop', isInteractive: true })
  await $.command.run({ command: 'terser-hud' })
}

test('band and pane show the Terser feed on every surface', async ($, on) => {
  const argvs: string[][] = []
  await setup($, on, argvs)
  expect(argvs.length > 0).toBe(true)
  expect(argvs[0]).toContain('--transcript')

  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const band = await $.ui.mount({ ...BAND, surface })
    expect(await band.find({ type: 'Text', text: /48\.2k/ })).toBeDefined()
    expect(await band.find({ key: 'details' })).toBeDefined()
    await band.unmount()

    const pane = await $.ui.mount({ ...PANE, surface })
    expect(await pane.find({ type: 'Text', text: /claude-opus-5-5/ })).toBeDefined()
    expect(await pane.find({ type: 'Text', text: /1\.7M/ })).toBeDefined()
    await pane.unmount()
  }
})

test('Hide removes the band', async ($, on) => {
  await setup($, on)
  const band = await $.ui.mount({ ...BAND, surface: 'desktop' })
  await band.press({ key: 'hide' })
  expect(await band.find({ key: 'details' })).toBeUndefined()
  await band.unmount()
})

test('the router dropdown switches Terser routing', async ($, on) => {
  const argvs: string[][] = []
  await setup($, on, argvs)
  for (const surface of ['terminal', 'desktop'] as const) {
    argvs.length = 0
    const band = await $.ui.mount({ ...BAND, surface })
    expect(await band.find({ key: 'router-model' })).toBeDefined()
    await band.select({ key: 'router-model', value: 'sonnet' })
    const cmds = argvs.map(a => a.slice(4).join(' '))
    expect(cmds).toContain('routing model sonnet')
    expect(cmds).toContain('routing on')
    argvs.length = 0
    await band.select({ key: 'router-model', value: 'off' })
    expect(argvs.map(a => a.slice(4).join(' '))).toContain('routing off')
    await band.unmount()

    argvs.length = 0
    const pane = await $.ui.mount({ ...PANE, surface })
    await pane.select({ key: 'pane-router-effort', value: 'medium' })
    expect(argvs.map(a => a.slice(4).join(' '))).toContain('routing effort medium')
    await pane.unmount()
  }
})

test('a Python failure names the file and line', async ($, on) => {
  await setup($, on, [], {
    exitCode: 1,
    stdout: '',
    stderr: '  File "C:/x/runtime/cli.py", line 235\n    print("oops)\nSyntaxError: unterminated string literal (detected at line 235)',
  })
  const pane = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await pane.find({ type: 'Text', text: /cli\.py:235/ })).toBeDefined()
  await pane.unmount()
})
