import { useMemo } from 'react'
import { useUi } from '@/store'

/** Resolved colour tokens for canvas-based chart libraries (they cannot read CSS variables). */
export interface ChartTokens {
  theme: 'dark' | 'light'
  bg: string
  surface: string
  surface2: string
  surface3: string
  border: string
  borderStrong: string
  fg: string
  muted: string
  faint: string
  grid: string
  accent: string
  accentSoft: string
  up: string
  down: string
  upSoft: string
  downSoft: string
  warn: string
  info: string
  tooltipBg: string
  /** fixed-order categorical palette (max 4 identities) */
  series: string[]
  /** neutral for peer-median / benchmark / reference marks */
  ref: string
  fontSans: string
  fontMono: string
}

function v(style: CSSStyleDeclaration, name: string, fallback: string): string {
  return style.getPropertyValue(name).trim() || fallback
}

export function readTokens(theme: 'dark' | 'light'): ChartTokens {
  const s = getComputedStyle(document.documentElement)
  return {
    theme,
    bg: v(s, '--bg', '#0d0f12'),
    surface: v(s, '--surface', '#14171b'),
    surface2: v(s, '--surface-2', '#1a1e23'),
    surface3: v(s, '--surface-3', '#22272e'),
    border: v(s, '--border', '#262b32'),
    borderStrong: v(s, '--border-strong', '#363c45'),
    fg: v(s, '--fg', '#e4e7eb'),
    muted: v(s, '--muted', '#9aa3ae'),
    faint: v(s, '--faint', '#69727e'),
    grid: v(s, '--grid', '#20252b'),
    accent: v(s, '--accent', '#ff6002'),
    accentSoft: v(s, '--accent-soft', 'rgba(255,96,2,.14)'),
    up: v(s, '--up', '#2bb8a3'),
    down: v(s, '--down', '#f25f7a'),
    upSoft: v(s, '--up-soft', 'rgba(43,184,163,.14)'),
    downSoft: v(s, '--down-soft', 'rgba(242,95,122,.14)'),
    warn: v(s, '--warn', '#e3b341'),
    info: v(s, '--info', '#6b9cf5'),
    tooltipBg: v(s, '--tooltip-bg', '#1f242a'),
    series: ['--c1', '--c2', '--c3', '--c4'].map((n, i) => v(s, n, ['#ef5a00', '#5b8be6', '#b88a1e', '#a27be0'][i])),
    ref: v(s, '--ref', '#8b949f'),
    fontSans: "'Inter Variable', Inter, 'Segoe UI', system-ui, sans-serif",
    fontMono: "'IBM Plex Mono', ui-monospace, Consolas, monospace",
  }
}

/** Tokens for the active theme; recomputed when the theme toggles. */
export function useChartTokens(): ChartTokens {
  const theme = useUi((s) => s.theme)
  return useMemo(() => readTokens(theme), [theme])
}

/** Convert a hex colour to rgba with alpha. */
export function alpha(hex: string, a: number): string {
  const h = hex.replace('#', '')
  if (h.length !== 6) return hex
  const n = parseInt(h, 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`
}

/** Diverging scale for heatmaps: down → neutral → up. */
export function divergingColors(t: ChartTokens): string[] {
  return [t.down, alpha(t.down, 0.45), t.surface3, alpha(t.up, 0.45), t.up]
}
