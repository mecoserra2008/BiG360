import { describe, expect, it } from 'vitest'
import {
  avgOffDiag,
  cellBackground,
  cellText,
  coverageOrder,
  curvePoints,
  fmtSignalRaw,
  gaugePos,
  groupRows,
  intensity,
  matrixCells,
  positioningLine,
  regimeTone,
  shapeRuns,
  shapeShares,
  sortedComponents,
  studioSeeds,
  thin,
  trimLeading,
} from '../adapters'
import type { XaCockpitRow, XaCoverage, XaRiskComponent, XaSignalDef } from '../types'
import { crossAssetDraft } from '@/pages/crossasset/draft'

const sig = (id: string, scale: 'z' | 'score' = 'z'): XaSignalDef => ({ id, label: id, z_field: scale === 'z' ? `z_${id}` : null, raw_field: id, scale, model_based: false, description: '' })

describe('heatmap cells', () => {
  it('maps z-scores and bounded scores to [-1, 1]', () => {
    expect(intensity(5, 'z')).toBe(1)
    expect(intensity(-1.25, 'z')).toBeCloseTo(-0.5)
    expect(intensity(0.4, 'score')).toBeCloseTo(0.4)
    expect(intensity(null, 'z')).toBeNull()
    expect(cellBackground(null, 'z')).toBe('var(--surface-2)')
    expect(cellBackground(2.5, 'z')).toContain('var(--c2) 62%')
    expect(cellBackground(-2.5, 'z')).toContain('var(--c1) 62%')
  })
  it('formats raw values by signal and value method', () => {
    expect(fmtSignalRaw('carry', 0.0234)).toBe('+2.3%')
    expect(fmtSignalRaw('momentum', -0.1)).toBe('−10.0%')
    expect(fmtSignalRaw('positioning', 0.913)).toBe('91')
    expect(fmtSignalRaw('iv_rv', 1.234)).toBe('1.23×')
    expect(fmtSignalRaw('vol', 0.183)).toBe('18.3%')
    expect(fmtSignalRaw('value', 1.85, { value_method: 'real_yield' })).toBe('1.85%')
    expect(fmtSignalRaw('value', -0.2, { value_method: 'reversal_5y' })).toBe('−20.0%')
    expect(fmtSignalRaw('trend', 0.456)).toBe('+0.46')
    expect(fmtSignalRaw('carry', null)).toBe('—')
  })
  it('shows z for z-coloured cells and the score for bounded ones', () => {
    expect(cellText(sig('carry'), { z: 1.26, raw: 0.02 })).toBe('+1.3')
    expect(cellText(sig('trend', 'score'), { z: null, raw: -0.31 })).toBe('−0.31')
    expect(cellText(sig('positioning'), { z: null, raw: 0.5 })).toBe('50')
    expect(cellText(sig('value'), { z: null, raw: null })).toBe('—')
    expect(cellText(sig('vol'), { z: -0.04, raw: 0.1 })).toBe('0.0')
  })
  it('groups rows by asset class in API order', () => {
    const rows = [{ asset_class: 'commodity', asset_id: 'CL' }, { asset_class: 'commodity', asset_id: 'GC' }, { asset_class: 'fx', asset_id: 'EURUSD' }] as XaCockpitRow[]
    const g = groupRows(rows)
    expect(g.map((x) => [x.asset_class, x.label, x.rows.length])).toEqual([
      ['commodity', 'Commodities', 2],
      ['fx', 'FX', 1],
    ])
  })
})

describe('positioning and curves', () => {
  it('labels positioning as CFTC (official) with the report date', () => {
    expect(positioningLine({ report_date: '2026-10-06', release_date: '2026-10-09', category: 'lev_money', inverted: false })).toBe(
      'CFTC (official), as of report date 6 Oct 2026 (released 9 Oct 2026) · leveraged funds',
    )
    expect(positioningLine({ report_date: '2026-10-06', release_date: null, category: 'lev_money', inverted: true })).toContain('inverted')
    expect(positioningLine(null)).toBe('No CFTC positioning for this asset')
  })
  it('turns a curve snapshot into months-to-expiry points', () => {
    const pts = curvePoints({
      points: [
        { contract: 'H27', contract_month: '2027-03-01', expiry: '2027-02-19', days_to_expiry: 122, close: 70 },
        { contract: 'Z26', contract_month: '2026-12-01', expiry: '2026-11-19', days_to_expiry: 30, close: 72 },
        { contract: 'F27', contract_month: '2027-01-01', expiry: '2026-12-18', days_to_expiry: 61, close: null },
      ],
    })
    expect(pts.map((p) => p.contract)).toEqual(['Z26', 'H27'])
    expect(pts[0].x).toBeCloseTo(0.99, 2)
  })
  it('orders coverage: live listing by month, expired last', () => {
    const c = (contract: string, month: string, status: XaCoverage['status']): XaCoverage => ({ contract, contract_month: month, symbol: contract, expiry: month, status })
    const out = coverageOrder([c('V26', '2026-10-01', 'expired'), c('F27', '2027-01-01', 'missing'), c('Z26', '2026-12-01', 'served'), c('U26', '2026-09-01', 'expired')])
    expect(out.map((x) => x.contract)).toEqual(['Z26', 'F27', 'V26', 'U26'])
  })
  it('summarises curve shapes', () => {
    const d = ['a', 'b', 'c', 'd', 'e']
    const s = ['contango', 'contango', null, 'backwardation', 'backwardation']
    expect(shapeRuns(d, s)).toEqual([
      { shape: 'contango', from: 'a', to: 'b', n: 2 },
      { shape: 'backwardation', from: 'd', to: 'e', n: 2 },
    ])
    expect(shapeShares(s)).toEqual({ contango: 0.5, backwardation: 0.5, flat: 0, n: 4 })
  })
})

describe('correlation and risk', () => {
  it('builds heatmap triples with the first asset on top and the average off-diagonal', () => {
    const m = [
      [1, 0.5],
      [0.5, 1],
    ]
    expect(matrixCells(m)).toEqual([
      [0, 1, 1],
      [1, 1, 0.5],
      [0, 0, 0.5],
      [1, 0, 1],
    ])
    expect(avgOffDiag(m)).toBeCloseTo(0.5)
    expect(avgOffDiag([[1]])).toBeNull()
  })
  it('positions the gauge and tones the regime', () => {
    expect(gaugePos(0)).toBe(0.5)
    expect(gaugePos(5)).toBe(1)
    expect(gaugePos(-1)).toBeCloseTo(0.25)
    expect(gaugePos(null)).toBeNull()
    expect(regimeTone('risk_on')).toBe('up')
    expect(regimeTone('risk_off')).toBe('down')
    expect(regimeTone(null)).toBe('neutral')
  })
  it('sorts components by z with nulls last', () => {
    const comps: XaRiskComponent[] = [
      { id: 'a', label: 'A', sign: 1, description: '', z: -1 },
      { id: 'b', label: 'B', sign: -1, description: '', z: null },
      { id: 'c', label: 'C', sign: 1, description: '', z: 2 },
    ]
    expect(sortedComponents({ components: comps }).map((c) => c.id)).toEqual(['c', 'a', 'b'])
  })
})

describe('Model Studio', () => {
  it('seeds a weekly OLS of the log return on lagged signals', () => {
    const s = studioSeeds({ asset_id: 'CL', name: 'WTI' }, true)
    expect(s.map((x) => x.field)).toEqual(['CL:price', 'CL:trend_signal', 'CL:carry', 'CL:cot_index_3y'])
    expect(s[0]).toMatchObject({ role: 'y', transforms: [{ op: 'log_diff' }] })
    expect(s.slice(1).every((x) => x.role === 'x' && x.transforms[0].op === 'lag')).toBe(true)
    expect(studioSeeds({ asset_id: 'DAX', name: 'DAX' }, false, false).map((x) => x.name)).toEqual(['ret', 'trend'])
    const dr = crossAssetDraft({ asset_id: 'CL', name: 'WTI' }, true)
    expect(dr.frequency).toBe('W')
    expect(dr.variables.every((v) => v.source === 'cross_asset' && v.entities === null)).toBe(true)
  })
  it('thins long arrays keeping the last point', () => {
    const xs = Array.from({ length: 1001 }, (_, i) => i)
    const t = thin(xs, 100)
    expect(t.length).toBeLessThanOrEqual(102)
    expect(t[t.length - 1]).toBe(1000)
    expect(thin([1, 2, 3], 10)).toEqual([1, 2, 3])
  })
})

describe('curve history trimming', () => {
  it('starts at the first date with a curve slope', () => {
    const t = trimLeading(['a', 'b', 'c'], { curve_slope: [null, 0.02, 0.03], front_close: [1, 2, 3] }, [null, 'contango', 'contango'], 'curve_slope')
    expect(t.dates).toEqual(['b', 'c'])
    expect(t.series.front_close).toEqual([2, 3])
    expect(t.shapes).toEqual(['contango', 'contango'])
    expect(trimLeading(['a'], { curve_slope: [null] }, [null], 'curve_slope').dates).toEqual(['a'])
  })
})
