export type Cat = { cat: string; saved_tok: number; events: number; pct: number }

export type Grade = { letter: string; score: number; reasons: string[] }

export type HudSession = {
  started_at?: number
  saved_tok?: number
  saved_usd?: number
  events?: number
  by_cat?: Cat[]
  model?: string
  turns?: number
  input_tok?: number
  output_tok?: number
  cache_read_tok?: number
  cache_write_tok?: number
  cost_usd?: number
  ctx_fill?: number | null
  ctx_window?: number
  peak_ctx?: number
  tool_calls?: number
  tool_errors?: number
  top_tools?: { tool: string; n: number }[]
  repeated_reads?: number
  quick_reruns?: number
  archive_expands?: number
  big_results?: number
  cache_hit?: number | null
  grade?: Grade
  error?: string
}

export type Hud = {
  version?: string
  generated_at?: number
  enabled?: boolean
  shadow?: boolean
  error?: string
  price?: {
    input_per_mtok: number
    output_per_mtok: number
    cache_write_mult: number
    cache_read_mult: number
    reread_turns: number
  }
  lifetime?: { saved_tok: number; saved_usd: number; events: number; by_cat: Cat[] }
  features?: Record<string, string | boolean | number | null>
  session?: HudSession | null
  index?: { repo?: string; files?: number; symbols?: number } | null
  funnel?: { tier: string; served?: number; retry?: number; mismatch?: number }[]
  routing?: {
    enabled: boolean
    model: string
    effort?: string
    verify: boolean
    routed?: number
    checked?: number
    failed?: number
    backed_off?: boolean
  }
}

declare module 'claude-code' {
  interface PluginState {
    'terser-hud': {
      hud: Shaped<Hud | null>
      fetchError: string | null
      isHidden: boolean
      autoOpened: boolean
    }
  }
}
