import type {
  EdgeType,
  ExposureBasis,
  MapOverlay,
  NodeColorBy,
  ScEdge,
  ScGeoCountry,
  ScGeography,
  ScNetwork,
  ScNode,
  ScOverlay,
  ScPortfolio,
  ScShockImpact,
} from './types'

export const EDGE_TYPE: Record<EdgeType, { label: string; disclosed: boolean; tip: string }> = {
  disclosed_customer: { label: 'Customer (disclosed by supplier)', disclosed: true, tip: 'The supplier names this customer in its annual report' },
  disclosed_supplier: { label: 'Supplier (disclosed by customer)', disclosed: true, tip: 'The customer names this supplier in its annual report (reverse edge)' },
  both: { label: 'Disclosed by both sides', disclosed: true, tip: 'Named by the supplier and by the customer' },
  statistical: { label: 'Statistical neighbour', disclosed: false, tip: 'Weekly return co-movement - not a disclosed relationship' },
}

export const COVERAGE: Record<string, { label: string; tone: 'up' | 'warn' | 'down' }> = {
  full: { label: 'Full coverage', tone: 'up' },
  partial: { label: 'Partial coverage', tone: 'warn' },
  sparse: { label: 'Sparse coverage', tone: 'down' },
}

export const OVERLAYS: { value: MapOverlay; label: string }[] = [
  { value: 'exposure', label: 'Revenue exposure' },
  { value: 'risk_score', label: 'Country risk' },
  { value: 'event_pressure', label: 'Event pressure' },
  { value: 'p_escalation_30d', label: 'Escalation 30d' },
  { value: 'recession_prob', label: 'Recession prob.' },
]

export function overlayLabel(o: MapOverlay): string {
  return OVERLAYS.find((x) => x.value === o)?.label ?? o
}

export const coverageOf = (level: string | null | undefined) => COVERAGE[level ?? 'sparse'] ?? COVERAGE.sparse

export const isUnresolved = (key: string) => key.startsWith('name:') || key.startsWith('cik:')

/** Value used to colour a graph node (null = no data, drawn neutral). */
export function nodeColorValue(n: ScNode, by: NodeColorBy): number | null {
  const v = by === 'score' ? n.score : by === 'event_pressure' ? n.event_pressure : n.country_risk
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** Edge strength in [0, 1] for line width: revenue / purchase share when disclosed, else |rho| or confidence. */
export function edgeStrength(e: ScEdge): number {
  const s = e.share_of_supplier_revenue ?? e.share_of_customer_purchases
  if (typeof s === 'number') return Math.min(1, Math.max(0, s))
  if (typeof e.rho === 'number') return Math.min(1, Math.abs(e.rho)) * 0.4
  return Math.min(1, Math.max(0, e.confidence)) * 0.15
}

export interface GraphNode {
  id: string
  name: string
  value: number | null
  symbolSize: number
  center: boolean
  unresolved: boolean
  securityId: string | null
  role: ScNode['role']
  depth: number
}
export interface GraphLink {
  source: string
  target: string
  type: EdgeType
  width: number
  dashed: boolean
  edge: ScEdge
}

/** Force-graph model: node size from market cap (log) with the center emphasised; links keep their contract edge. */
export function graphModel(net: ScNetwork, colorBy: NodeColorBy, showStatistical = true): { nodes: GraphNode[]; links: GraphLink[]; range: [number, number] } {
  const caps = net.nodes.map((n) => n.market_cap_usd ?? 0).filter((c) => c > 0)
  const lo = caps.length ? Math.log10(Math.min(...caps)) : 0
  const hi = caps.length ? Math.log10(Math.max(...caps)) : 1
  const links = net.edges
    .filter((e) => showStatistical || e.type !== 'statistical')
    .map((e) => ({ source: e.source, target: e.target, type: e.type, width: 0.8 + 5 * edgeStrength(e), dashed: e.type === 'statistical' || e.stale, edge: e }))
  const keep = new Set([net.center_key, ...links.flatMap((l) => [l.source, l.target])])
  const nodes = net.nodes
    .filter((n) => keep.has(n.key))
    .map((n) => {
      const c = n.market_cap_usd && n.market_cap_usd > 0 && hi > lo ? (Math.log10(n.market_cap_usd) - lo) / (hi - lo) : 0.3
      const center = n.key === net.center_key
      return {
        id: n.key,
        name: n.ticker ?? n.name,
        value: nodeColorValue(n, colorBy),
        symbolSize: center ? 34 : n.kind === 'unresolved' ? 10 : 12 + 16 * c,
        center,
        unresolved: n.kind === 'unresolved',
        securityId: n.security_id ?? null,
        role: n.role,
        depth: n.depth,
      }
    })
  const vals = nodes.map((n) => n.value).filter((v): v is number => v !== null)
  const range: [number, number] = colorBy === 'score' ? [0, 100] : vals.length ? [Math.min(...vals), Math.max(...vals)] : [0, 1]
  return { nodes, links, range }
}

/** Share of a basis (null-safe) for one country row. */
export function basisValue(c: { direct?: number | null; second_order?: number | null; look_through?: number | null }, basis: ExposureBasis): number | null {
  const v = basis === 'direct' ? c.direct : basis === 'second_order' ? c.second_order : c.look_through
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

export function overlayValue(o: ScOverlay | null | undefined, k: Exclude<MapOverlay, 'exposure'>): number | null {
  const v = o ? o[k] : null
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** Map series data for the revenue-geography choropleth. */
export function geoMapData(rows: { iso2: string; direct?: number | null; second_order?: number | null; look_through?: number | null; overlay?: ScOverlay | null }[], overlay: MapOverlay, basis: ExposureBasis): { name: string; value: number }[] {
  const out: { name: string; value: number }[] = []
  for (const r of rows) {
    const v = overlay === 'exposure' ? basisValue(r, basis) : overlayValue(r.overlay, overlay)
    if (v !== null && (overlay !== 'exposure' || v > 0)) out.push({ name: r.iso2, value: v })
  }
  return out
}

/** Exposure table rows sorted by the chosen basis (largest first), zero rows dropped. */
export function exposureRows(geo: ScGeography, basis: ExposureBasis): ScGeoCountry[] {
  return [...geo.countries].filter((c) => (basisValue(c, basis) ?? 0) > 0 || (basis === 'direct' && c.source === 'home_country')).sort((a, b) => (basisValue(b, basis) ?? 0) - (basisValue(a, basis) ?? 0))
}

/** Exposure-weighted average of an overlay field over the chosen basis (countries without the field are skipped). */
export function weightedOverlay(rows: { direct?: number | null; second_order?: number | null; look_through?: number | null; overlay?: ScOverlay | null }[], k: Exclude<MapOverlay, 'exposure'>, basis: ExposureBasis): number | null {
  let num = 0
  let den = 0
  for (const r of rows) {
    const w = basisValue(r, basis)
    const v = overlayValue(r.overlay, k)
    if (w && w > 0 && v !== null) {
      num += w * v
      den += w
    }
  }
  return den > 0 ? num / den : null
}

/** Human-readable propagation path ("NVDA → TSMC"). */
export function pathText(x: ScShockImpact): string {
  return x.path.length > 1 ? x.path.join(' → ') : 'shocked node'
}

/** Shock impacts split for display: affected companies (excluding the seed node) sorted by |impact|. */
export function shockRows(rows: ScShockImpact[], includeSeed = false): ScShockImpact[] {
  return rows.filter((r) => includeSeed || r.channel !== 'seed').sort((a, b) => Math.abs(b.revenue_impact) - Math.abs(a.revenue_impact))
}

/** Portfolio countries -> map data on the chosen basis / overlay. */
export function portfolioMapData(pe: ScPortfolio, overlay: MapOverlay, basis: 'direct' | 'look_through'): { name: string; value: number }[] {
  return geoMapData(pe.countries, overlay, basis)
}

export const HOTSPOT_TEXT: Record<string, string> = {
  hot99: 'Hot spot 99%',
  hot95: 'Hot spot 95%',
  hot90: 'Hot spot 90%',
  cold99: 'Cold spot 99%',
  cold95: 'Cold spot 95%',
  cold90: 'Cold spot 90%',
  ns: 'Not significant',
}
