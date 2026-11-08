import { describe, expect, it } from 'vitest'
import { coverageOf, edgeStrength, exposureRows, geoMapData, graphModel, isUnresolved, nodeColorValue, pathText, shockRows, weightedOverlay } from '../adapters'
import type { ScEdge, ScGeoCountry, ScGeography, ScNetwork, ScNode, ScShockImpact } from '../types'

const prov = { filing_id: 'f', form_type: '10-K', snippet: 's', knowledge_date: '2026-02-01', period_end: '2025-12-31', first_knowledge_date: null, rules: ['c_name_pct'], sources: ['text_10k'], n_disclosures: 1 }
const edge = (p: Partial<ScEdge>): ScEdge => ({ source: 'A', target: 'B', type: 'disclosed_customer', confidence: 0.9, stale: false, provenance: prov, share_of_supplier_revenue: null, share_of_customer_purchases: null, rho: null, ...p })
const node = (p: Partial<ScNode>): ScNode => ({ key: 'A', name: 'Alpha', kind: 'issuer', depth: 1, role: 'customer', ...p })

const net: ScNetwork = {
  security: { security_id: 'S1', issuer_id: 'A', ticker: 'AAA', name: 'Alpha', region: 'US', country: 'US', currency: 'USD' } as ScNetwork['security'],
  as_of: '2026-10-16',
  center_key: 'A',
  depth: 1,
  nodes: [
    node({ key: 'A', role: 'center', depth: 0, market_cap_usd: 1e12, score: 70 }),
    node({ key: 'B', name: 'Beta', market_cap_usd: 1e9, score: 40, event_pressure: 1.5 }),
    node({ key: 'name:acme', name: 'Acme', kind: 'unresolved' }),
    node({ key: 'N', name: 'Nu', role: 'neighbour', market_cap_usd: 1e10 }),
  ],
  edges: [
    edge({ source: 'A', target: 'B', share_of_supplier_revenue: 0.3 }),
    edge({ source: 'A', target: 'name:acme', confidence: 0.36 }),
    edge({ source: 'A', target: 'N', type: 'statistical', rho: 0.8, confidence: 0.8 }),
  ],
  metrics: null,
  coverage: { level: 'partial', has_disclosed_links: true, has_geo_revenue: false, n_neighbours_stat: 1, note: 'x' },
  labels: { disclosed: '', statistical: '', model_based: '', country_risk: '', look_through: '', shock: '' },
  data_status: 'ok',
}

describe('network graph model', () => {
  it('sizes, colours and filters statistical edges', () => {
    const g = graphModel(net, 'score')
    expect(g.nodes.find((n) => n.id === 'A')?.center).toBe(true)
    expect(g.nodes.find((n) => n.id === 'A')?.symbolSize).toBe(34)
    expect(g.nodes.find((n) => n.id === 'name:acme')?.unresolved).toBe(true)
    expect(g.range).toEqual([0, 100])
    expect(g.links.find((l) => l.type === 'statistical')?.dashed).toBe(true)
    const ns = graphModel(net, 'event_pressure', false)
    expect(ns.links.some((l) => l.type === 'statistical')).toBe(false)
    expect(ns.nodes.some((n) => n.id === 'N')).toBe(false)
    expect(ns.nodes.find((n) => n.id === 'B')?.value).toBe(1.5)
  })
  it('edge strength prefers shares, then rho, then confidence', () => {
    expect(edgeStrength(edge({ share_of_supplier_revenue: 0.3 }))).toBe(0.3)
    expect(edgeStrength(edge({ share_of_customer_purchases: 2 }))).toBe(1)
    expect(edgeStrength(edge({ type: 'statistical', rho: -0.5 }))).toBeCloseTo(0.2)
    expect(edgeStrength(edge({ confidence: 1 }))).toBeCloseTo(0.15)
  })
  it('helpers', () => {
    expect(isUnresolved('name:x')).toBe(true)
    expect(isUnresolved('I123')).toBe(false)
    expect(nodeColorValue(node({ country_risk: 55 }), 'country_risk')).toBe(55)
    expect(nodeColorValue(node({}), 'score')).toBeNull()
    expect(coverageOf('full').tone).toBe('up')
    expect(coverageOf(undefined).label).toBe('Sparse coverage')
  })
})

const c = (p: Partial<ScGeoCountry>): ScGeoCountry => ({ iso2: 'US', n_paths: 0, via: [], source: 'geo_revenue', direct: null, second_order: null, look_through: null, overlay: null, ...p })

describe('geography adapters', () => {
  const geo = {
    countries: [
      c({ iso2: 'US', direct: 0.6, look_through: 0.5, overlay: { n_components: 2, risk_score: 20, recession_prob: 0.1 } }),
      c({ iso2: 'CN', direct: 0.4, look_through: 0.3, overlay: { n_components: 3, risk_score: 80 } }),
      c({ iso2: 'FR', direct: 0, second_order: 0.2, look_through: 0.2, overlay: null }),
    ],
  } as unknown as ScGeography
  it('orders and filters exposure rows by basis', () => {
    expect(exposureRows(geo, 'direct').map((r) => r.iso2)).toEqual(['US', 'CN'])
    expect(exposureRows(geo, 'look_through').map((r) => r.iso2)).toEqual(['US', 'CN', 'FR'])
    expect(exposureRows(geo, 'second_order').map((r) => r.iso2)).toEqual(['FR'])
  })
  it('builds map data for exposure and overlays', () => {
    expect(geoMapData(geo.countries, 'exposure', 'direct')).toEqual([{ name: 'US', value: 0.6 }, { name: 'CN', value: 0.4 }])
    expect(geoMapData(geo.countries, 'risk_score', 'direct')).toEqual([{ name: 'US', value: 20 }, { name: 'CN', value: 80 }])
  })
  it('weights overlays by exposure', () => {
    expect(weightedOverlay(geo.countries, 'risk_score', 'direct')).toBeCloseTo(0.6 * 20 + 0.4 * 80)
    expect(weightedOverlay(geo.countries, 'recession_prob', 'direct')).toBeCloseTo(0.1)
    expect(weightedOverlay(geo.countries, 'gpr' as never, 'direct')).toBeNull()
  })
})

describe('shock adapters', () => {
  const imp = (p: Partial<ScShockImpact>): ScShockImpact => ({ key: 'X', name: 'X', revenue_impact: 0, by_hop: [], first_hop: 1, channel: 'demand', path: ['A', 'X'], path_keys: ['A', 'X'], ...p })
  it('sorts by absolute impact and drops the seed', () => {
    const rows = shockRows([imp({ key: 'A', channel: 'seed', revenue_impact: -0.2, path: ['A'] }), imp({ key: 'B', revenue_impact: -0.01 }), imp({ key: 'C', revenue_impact: 0.05 })])
    expect(rows.map((r) => r.key)).toEqual(['C', 'B'])
    expect(pathText(imp({ path: ['Alpha', 'Beta', 'Gamma'] }))).toBe('Alpha → Beta → Gamma')
    expect(pathText(imp({ path: ['Alpha'] }))).toBe('shocked node')
  })
})
