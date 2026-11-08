import { Info, Network, Zap } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { useScenarios } from '@/api/macro/hooks'
import { EDGE_TYPE, HOTSPOT_TEXT, OVERLAYS, basisValue, overlayLabel, coverageOf, exposureRows, geoMapData, graphModel, pathText, shockRows } from '@/api/supplychain/adapters'
import { useCompanyGeography, useCompanyNetwork, useShock } from '@/api/supplychain/hooks'
import type { EdgeType, ExposureBasis, MapOverlay, NetworkQuery, NodeColorBy, ScCoverage, ScEdge, ScGeography, ScMetrics, ScNetwork, ScShockRequest, ScShockResult } from '@/api/supplychain/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Flag } from '@/components/ui/data'
import { EmptyState, ErrorState, PendingState, SkeletonRows } from '@/components/ui/feedback'
import { Checkbox, Select, Segmented, Slider } from '@/components/ui/inputs'
import { Panel, Stat } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDate, fmtInt, fmtNum, fmtPct } from '@/lib/format'
import { securityRoute } from '@/lib/routes'
import { countryName } from '@/lib/regions'
import { ExposureMap } from '../../supplychain/ExposureMap'


export default function NetworkTab({ id }: { id: string }) {
  const [q, setQ] = useState<NetworkQuery>({ depth: 1, includeStatistical: true, statisticalMethod: 'return_corr', minConfidence: 0 })
  const net = useCompanyNetwork(id, q)
  const geo = useCompanyGeography(id)
  if (net.isError) return <ErrorState error={net.error} onRetry={() => void net.refetch()} />
  if (!net.data) return <SkeletonRows rows={10} className="pt-3" />
  if (net.data.data_status === 'pending') return <PendingState detail="The supply-chain jobs (supply_chain_network) have not produced data for this snapshot yet." />
  return (
    <div className="space-y-3 pt-3">
      <Summary net={net.data} geo={geo.data} />
      <div className="grid gap-3 2xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <GraphPanel net={net.data} q={q} setQ={setQ} loading={net.isFetching} />
        <RelationsPanel net={net.data} />
      </div>
      {geo.isError ? <ErrorState error={geo.error} onRetry={() => void geo.refetch()} /> : geo.data ? <GeographyPanels geo={geo.data} /> : <SkeletonRows rows={6} />}
      <ShockPanel id={id} net={net.data} geo={geo.data} />
    </div>
  )
}

/* ─────────────────────────────────────────── summary + coverage ── */

export function CoverageBadge({ cov, metrics }: { cov: ScCoverage; metrics?: ScMetrics | null }) {
  const c = coverageOf(cov.level)
  const geoNote = cov.has_geo_revenue
    ? cov.geo_method === 'ixbrl_textblock_table'
      ? `Geography parsed from the ESEF segment-note table (rule-based, confidence ${fmtNum(metrics?.geo_confidence ?? 0.7, 1)})`
      : 'Geography from tagged XBRL segment facts'
    : 'No geographic revenue disclosure found: home country used'
  return (
    <Tip content={`${cov.note}. ${geoNote}.`}>
      <span tabIndex={0} className="inline-flex cursor-help items-center gap-1">
        <Badge tone={c.tone}>{c.label}</Badge>
        <Info className="size-3 text-muted" aria-hidden />
      </span>
    </Tip>
  )
}

function Summary({ net, geo }: { net: ScNetwork; geo?: ScGeography }) {
  const m = net.metrics
  return (
    <Panel
      title="Supply chain & revenue geography"
      subtitle={
        <span className="inline-flex flex-wrap items-center gap-2">
          <CoverageBadge cov={net.coverage} metrics={m} />
          <span className="text-2xs text-muted">Disclosed links from annual reports (rules, no LLM) · statistical neighbours labelled · risk and look-through are model-based</span>
        </span>
      }
    >
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-8">
        <Stat label="Customers" value={fmtInt(m?.n_customers ?? 0)} sub={`${fmtInt(m?.n_customers_resolved ?? 0)} matched to issuers`} />
        <Stat label="Suppliers" value={fmtInt(m?.n_suppliers ?? 0)} sub={`${fmtInt(m?.n_suppliers_resolved ?? 0)} matched`} />
        <Stat label="Top customer" value={fmtPct(m?.top_customer_share, 0)} sub={m?.top_customer_name ?? 'of revenue'} />
        <Stat label="Customer HHI" value={fmtNum(m?.customer_hhi, 3)} sub={`disclosed ${fmtPct(m?.disclosed_customer_share, 0)} of revenue`} />
        <Stat label="PageRank" value={fmtNum(m?.pagerank, 2)} sub="× average node" />
        <Stat label="Top country" value={m?.top_country ? `${m.top_country} ${fmtPct(m.top_country_share, 0)}` : DASH} sub={`${fmtInt(m?.n_countries ?? 0)} countries · HHI ${fmtNum(m?.geo_hhi, 2)}`} />
        <Stat label="Country risk*" value={fmtNum(m?.country_risk, 0)} sub={`look-through ${fmtNum(m?.country_risk_look_through, 0)}`} />
        <Stat label="Geo breakdown" value={m?.geo_source === 'geo_revenue' ? fmtDate(m?.geo_period_end) : 'home only'} sub={m?.geo_knowledge_date ? `filed ${fmtDate(m.geo_knowledge_date)}` : (geo?.coverage.note ?? undefined)} />
      </div>
      <p className="mt-2 text-2xs text-faint">* {net.labels.country_risk}.</p>
    </Panel>
  )
}

/* ─────────────────────────────────────────── force graph ── */

const COLOR_BY: { value: NodeColorBy; label: string }[] = [
  { value: 'score', label: 'Score' },
  { value: 'event_pressure', label: 'Events' },
  { value: 'country_risk', label: 'Country risk' },
]

function edgeColor(t: ReturnType<typeof useChartTokens>, type: EdgeType): string {
  return type === 'disclosed_customer' ? t.accent : type === 'disclosed_supplier' ? t.series[1] : type === 'both' ? t.up : t.ref
}

function GraphPanel({ net, q, setQ, loading }: { net: ScNetwork; q: NetworkQuery; setQ: (q: NetworkQuery) => void; loading: boolean }) {
  const t = useChartTokens()
  const navigate = useNavigate()
  const [colorBy, setColorBy] = useState<NodeColorBy>('score')
  const g = useMemo(() => graphModel(net, colorBy, q.includeStatistical), [net, colorBy, q.includeStatistical])
  const byKey = useMemo(() => new Map(net.nodes.map((n) => [n.key, n])), [net.nodes])
  const option = useMemo<EChartsOption>(() => {
    const [lo, hi] = g.range
    const ramp = colorBy === 'score' ? [t.down, t.warn, t.up] : [alpha(t.warn, 0.35), t.warn, t.down]
    const color = (v: number | null) => {
      if (v === null) return t.surface3
      const x = hi > lo ? Math.min(1, Math.max(0, (v - lo) / (hi - lo))) : 0.5
      return x < 0.5 ? ramp[0] : x < 0.8 ? ramp[1] : ramp[2]
    }
    return {
      tooltip: {
        trigger: 'item',
        confine: true,
        formatter: (p: unknown) => {
          const x = p as { dataType: string; data: { id?: string; edge?: ScEdge } }
          if (x.dataType === 'edge' && x.data.edge) {
            const e = x.data.edge
            const pv = e.provenance
            let s = ttTitle(`${byKey.get(e.source)?.name ?? e.source} → ${byKey.get(e.target)?.name ?? e.target}`)
            s += ttRow(edgeColor(t, e.type), 'Type', EDGE_TYPE[e.type].label)
            if (e.share_of_supplier_revenue != null) s += ttRow(t.fg, 'Share of supplier revenue', fmtPct(e.share_of_supplier_revenue, 1))
            if (e.share_of_customer_purchases != null) s += ttRow(t.fg, 'Share of customer purchases', fmtPct(e.share_of_customer_purchases, 1))
            if (e.rho != null) s += ttRow(t.fg, 'Return correlation', fmtNum(e.rho, 2))
            s += ttRow(t.ref, 'Confidence', fmtNum(e.confidence, 2))
            if (pv.filing_id) s += ttRow(t.ref, 'Filing', `${pv.form_type ?? ''} ${fmtDate(pv.knowledge_date)}`)
            if (pv.snippet) s += `<div style="max-width:320px;white-space:normal;margin-top:4px;color:${t.muted}">“${pv.snippet.replace(/</g, '&lt;')}”</div>`
            return s
          }
          const n = byKey.get(x.data.id ?? '')
          if (!n) return ''
          let s = ttTitle(`${n.name}${n.ticker ? ` (${n.ticker})` : ''}`)
          s += ttRow(t.fg, 'Role', `${n.role} · depth ${n.depth}`)
          if (n.country) s += ttRow(t.fg, 'Country', n.country)
          s += ttRow(t.fg, 'Score', fmtNum(n.score, 0))
          s += ttRow(t.fg, 'Event pressure', fmtNum(n.event_pressure, 2))
          s += ttRow(t.fg, 'Country risk*', fmtNum(n.country_risk, 0))
          if (n.kind === 'unresolved') s += `<div style="color:${t.muted}">Counterparty not matched to a listed issuer</div>`
          else if (n.security_id) s += `<div style="color:${t.muted}">Click to open Company 360</div>`
          return s
        },
      },
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true,
          draggable: true,
          force: { repulsion: 220, edgeLength: [60, 150], gravity: 0.12 },
          label: { show: true, position: 'right', fontSize: 10, color: t.fg, formatter: '{b}' },
          edgeSymbol: ['none', 'arrow'],
          edgeSymbolSize: 6,
          data: g.nodes.map((n) => ({
            id: n.id,
            name: n.name,
            symbolSize: n.symbolSize,
            symbol: n.unresolved ? 'diamond' : 'circle',
            itemStyle: {
              color: n.center ? t.accent : color(n.value),
              borderColor: n.center ? t.fg : t.surface,
              borderWidth: n.center ? 2 : 1,
              opacity: n.depth >= 2 ? 0.75 : 1,
            },
            label: { fontWeight: n.center ? 700 : 400 },
          })),
          links: g.links.map((l) => ({
            source: l.source,
            target: l.target,
            edge: l.edge,
            lineStyle: { width: l.width, color: alpha(edgeColor(t, l.type), l.type === 'statistical' ? 0.55 : 0.8), type: l.dashed ? 'dashed' : 'solid', curveness: 0.12 },
          })),
          emphasis: { focus: 'adjacency', lineStyle: { width: 4 } },
        },
      ],
    } as EChartsOption
  }, [g, t, colorBy, byKey])
  const legend: EdgeType[] = ['disclosed_customer', 'disclosed_supplier', 'both', 'statistical']
  return (
    <Panel
      title="Supply-chain network"
      subtitle={<span className="text-2xs text-muted">Arrows point supplier → customer · edge width = revenue / purchase share (else confidence) · click a company to open it</span>}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Segmented<'1' | '2'> label="Depth" size="xs" value={String(q.depth) as '1' | '2'} onChange={(v) => setQ({ ...q, depth: v === '2' ? 2 : 1 })} options={[{ value: '1', label: 'Depth 1' }, { value: '2', label: 'Depth 2' }]} />
          <Segmented<NodeColorBy> label="Colour nodes by" size="xs" value={colorBy} onChange={setColorBy} options={COLOR_BY} />
          <Segmented<'0' | '0.5' | '0.8'> label="Minimum confidence" size="xs" value={String(q.minConfidence) as '0' | '0.5' | '0.8'} onChange={(v) => setQ({ ...q, minConfidence: Number(v) })} options={[{ value: '0', label: 'All' }, { value: '0.5', label: '≥ 0.5' }, { value: '0.8', label: '≥ 0.8' }]} />
          <Checkbox label="Statistical neighbours" checked={q.includeStatistical} onCheckedChange={(v) => setQ({ ...q, includeStatistical: v === true })} />
          <span className="text-2xs text-muted">Statistical</span>
        </div>
      }
      bodyClassName="p-1.5"
    >
      {g.links.length ? (
        <EChart
          option={option}
          height={460}
          ariaLabel="Force-directed supply-chain graph"
          onEvents={{
            click: (p: unknown) => {
              const x = p as { dataType?: string; data?: { id?: string } }
              if (x.dataType !== 'node' || !x.data?.id) return
              const n = byKey.get(x.data.id)
              if (n?.security_id && n.key !== net.center_key) navigate(securityRoute(n.security_id))
            },
          }}
        />
      ) : (
        <EmptyState icon={<Network className="size-5" />} title="No disclosed supply-chain links" message="No customer / supplier disclosures were found for this company. Turn on statistical neighbours to see the return co-movement layer." className="h-[300px]" />
      )}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-2 pb-1 text-2xs text-muted">
        {legend.map((k) => (
          <Tip key={k} content={EDGE_TYPE[k].tip}>
            <span tabIndex={0} className="inline-flex cursor-help items-center gap-1.5">
              <span className="inline-block h-0 w-5 border-t-2" style={{ borderColor: edgeColor(t, k), borderStyle: k === 'statistical' ? 'dashed' : 'solid' }} />
              {EDGE_TYPE[k].label}
            </span>
          </Tip>
        ))}
        <span className="inline-flex items-center gap-1.5"><span className="inline-block size-2 rotate-45 bg-surface-3" /> unmatched counterparty</span>
        {loading && <span>Updating…</span>}
      </div>
    </Panel>
  )
}

/* ─────────────────────────────────────────── relation list (provenance) ── */

function RelationsPanel({ net }: { net: ScNetwork }) {
  const [kind, setKind] = useState<'customers' | 'suppliers' | 'statistical'>('customers')
  const byKey = useMemo(() => new Map(net.nodes.map((n) => [n.key, n])), [net.nodes])
  const navigate = useNavigate()
  const rows = net.edges.filter((e) =>
    kind === 'statistical' ? e.type === 'statistical' : kind === 'customers' ? e.type !== 'statistical' && e.source === net.center_key : e.type !== 'statistical' && e.target === net.center_key,
  )
  return (
    <Panel
      title="Relationships & provenance"
      actions={<Segmented label="Relation" size="xs" value={kind} onChange={setKind} options={[{ value: 'customers', label: 'Customers' }, { value: 'suppliers', label: 'Suppliers' }, { value: 'statistical', label: 'Statistical' }]} />}
      flush
    >
      {rows.length ? (
        <div className="max-h-[500px] overflow-auto">
          <table className="w-full text-xs" aria-label="Supply-chain relationships">
            <thead className="sticky top-0 bg-surface text-left text-muted">
              <tr className="border-b border-border">
                {['Company', kind === 'statistical' ? 'ρ' : 'Share', 'Conf.', 'Source'].map((h, i) => <th key={h} className={`px-2 py-1.5 font-medium ${i && i < 3 ? 'text-right' : ''}`}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => {
                const other = byKey.get(kind === 'suppliers' ? e.source : e.target)
                const share = kind === 'customers' ? e.share_of_supplier_revenue : e.share_of_customer_purchases
                return (
                  <tr key={`${e.source}-${e.target}-${e.type}`} className="border-b border-border/60 align-top">
                    <td className="max-w-[220px] px-2 py-1">
                      {other?.security_id ? (
                        <button type="button" className="truncate text-left font-medium text-accent-text hover:underline" onClick={() => navigate(securityRoute(other.security_id as string))}>{other.name}</button>
                      ) : (
                        <span className="truncate font-medium" title="Not matched to a listed issuer">{other?.name ?? DASH}</span>
                      )}
                      <div className="text-2xs text-muted">{EDGE_TYPE[e.type].label}{e.stale ? ' · stale (> 2y)' : ''}</div>
                      {e.provenance.snippet && <div className="mt-0.5 line-clamp-2 text-2xs text-faint" title={e.provenance.snippet}>“{e.provenance.snippet}”</div>}
                    </td>
                    <td className="num px-2 py-1 text-right">{kind === 'statistical' ? fmtNum(e.rho, 2) : fmtPct(share, 1)}</td>
                    <td className="num px-2 py-1 text-right">{fmtNum(e.confidence, 2)}</td>
                    <td className="px-2 py-1 text-2xs text-muted">
                      {e.provenance.form_type ?? (kind === 'statistical' ? 'prices' : DASH)} {e.provenance.knowledge_date ? fmtDate(e.provenance.knowledge_date) : ''}
                      <div className="text-faint">{e.provenance.rules.join(', ')}</div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState title={kind === 'statistical' ? 'No statistical neighbours' : `No disclosed ${kind}`} className="py-8" />
      )}
      <p className="border-t border-border px-2 py-1.5 text-2xs text-faint">{kind === 'statistical' ? net.labels.statistical : net.labels.disclosed}</p>
    </Panel>
  )
}

/* ─────────────────────────────────────────── geography ── */

const BASIS: { value: ExposureBasis; label: string; title: string }[] = [
  { value: 'direct', label: 'Direct', title: 'Own revenue by country' },
  { value: 'second_order', label: '2nd order', title: "Via identified customers' countries" },
  { value: 'look_through', label: 'Look-through', title: 'Revenue sold to identified customers replaced by their geography (model-based)' },
]

function GeographyPanels({ geo }: { geo: ScGeography }) {
  const [overlay, setOverlay] = useState<MapOverlay>('exposure')
  const [basis, setBasis] = useState<ExposureBasis>('direct')
  const rows = useMemo(() => exposureRows(geo, basis), [geo, basis])
  const data = useMemo(() => geoMapData(geo.countries, overlay, basis), [geo.countries, overlay, basis])
  const mapRows = useMemo(() => geo.countries.map((c) => ({ iso2: c.iso2, share: basisValue(c, basis), overlay: c.overlay })), [geo.countries, basis])
  return (
    <div className="grid gap-3 2xl:grid-cols-2">
      <Panel
        title="Revenue geography"
        subtitle={<span className="inline-flex flex-wrap items-center gap-2"><CoverageBadge cov={geo.coverage} metrics={geo.metrics} /><span className="text-2xs text-muted">{overlay === 'exposure' ? `${BASIS.find((b) => b.value === basis)?.title}` : `${overlayLabel(overlay)} (model-based overlay) on exposed countries`}</span></span>}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Segmented<ExposureBasis> label="Exposure basis" size="xs" value={basis} onChange={setBasis} options={BASIS} />
            <Select<MapOverlay> label="Map layer" value={overlay} onChange={setOverlay} options={OVERLAYS} className="w-40" />
          </div>
        }
        bodyClassName="p-1.5"
      >
        {data.length ? <ExposureMap rows={mapRows} data={data} overlay={overlay} ariaLabel={`Revenue geography map: ${overlayLabel(overlay)}`} /> : <EmptyState title="No values for this layer" message={overlay === 'exposure' ? 'No exposure on this basis (second-order needs customers with disclosed shares).' : 'No overlay data for the exposed countries.'} className="h-[300px]" />}
      </Panel>
      <Panel title="Exposure by country" subtitle={<span className="text-2xs text-muted">{geo.labels.look_through}</span>} flush>
        <div className="max-h-[420px] overflow-auto">
          <table className="w-full text-xs" aria-label="Country exposure with event and macro overlays">
            <thead className="sticky top-0 bg-surface text-left text-muted">
              <tr className="border-b border-border">
                {['Country', 'Direct', '2nd order', 'Look-through', 'Risk*', 'Events', 'Escal. 30d', 'Hotspot', 'Recession', 'Surprise', 'Via'].map((h, i) => <th key={h} className={`px-2 py-1.5 font-medium whitespace-nowrap ${i && i < 7 ? 'text-right' : i === 8 || i === 9 ? 'text-right' : ''}`}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => {
                const o = c.overlay
                return (
                  <tr key={c.iso2} className="border-b border-border/60">
                    <td className="px-2 py-1 whitespace-nowrap"><Flag country={c.iso2} /> <span title={countryName(c.iso2)}>{c.iso2}</span>{c.source === 'home_country' && <Badge tone="warn" className="ml-1" title="No geographic disclosure: home country">home</Badge>}</td>
                    <td className="num px-2 py-1 text-right">{fmtPct(c.direct, 1)}</td>
                    <td className="num px-2 py-1 text-right">{fmtPct(c.second_order, 1)}</td>
                    <td className="num px-2 py-1 text-right">{fmtPct(c.look_through, 1)}</td>
                    <td className="num px-2 py-1 text-right">{fmtNum(o?.risk_score, 0)}</td>
                    <td className="num px-2 py-1 text-right">{fmtNum(o?.event_pressure, 2)}</td>
                    <td className="num px-2 py-1 text-right">{fmtPct(o?.p_escalation_30d, 0)}</td>
                    <td className="px-2 py-1 text-2xs text-muted whitespace-nowrap">{o?.hotspot && o.hotspot !== 'ns' ? HOTSPOT_TEXT[o.hotspot] ?? o.hotspot : DASH}</td>
                    <td className="num px-2 py-1 text-right" title={o?.recession_method ?? undefined}>{fmtPct(o?.recession_prob, 0)}{o?.recession_official ? '†' : ''}</td>
                    <td className="num px-2 py-1 text-right">{fmtNum(o?.surprise_index, 0)}</td>
                    <td className="max-w-[160px] truncate px-2 py-1 text-2xs text-muted" title={c.via.map((v) => `${v.name ?? v.key} ${fmtPct(v.share, 0)}`).join(', ')}>{c.via.map((v) => v.name ?? v.key).join(', ') || DASH}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <p className="border-t border-border px-2 py-1.5 text-2xs text-faint">* Model-based country risk 0–100 · † official recession probability (NY Fed) · events = GDELT pressure (model-based)</p>
      </Panel>
      {geo.sectors.length > 1 && (
        <Panel title="Sector exposure via customers" subtitle={<span className="text-2xs text-muted">Own sector = direct; customers' sectors weighted by disclosed revenue share</span>} className="2xl:col-span-2" flush>
          <div className="flex flex-wrap gap-2 p-2 text-xs">
            {geo.sectors.map((s) => (
              <span key={s.sector_id} className="inline-flex items-center gap-1 rounded-sm border border-border px-2 py-0.5">
                <span className="font-medium">{s.sector_id}</span>
                <span className="num text-muted">{fmtPct(s.look_through, 1)}</span>
                {s.direct ? <Badge tone="neutral">own</Badge> : null}
              </span>
            ))}
          </div>
        </Panel>
      )}
    </div>
  )
}

/* ─────────────────────────────────────────── shock scenario ── */

function ShockPanel({ id, net, geo }: { id: string; net: ScNetwork; geo?: ScGeography }) {
  const sh = useShock()
  const scen = useScenarios()
  const [target, setTarget] = useState<'company' | 'country'>('company')
  const [iso, setIso] = useState<string>('')
  const [mag, setMag] = useState(-10)
  const [damping, setDamping] = useState(50)
  const [hops, setHops] = useState<'1' | '2' | '3'>('3')
  const [supply, setSupply] = useState(true)
  const [stat, setStat] = useState(false)
  const [homeToo, setHomeToo] = useState(false)
  const [macro, setMacro] = useState<string>('')
  const countries = useMemo(() => (geo?.countries ?? []).map((c) => c.iso2), [geo])
  const country = iso || countries[0] || ''
  const presets = scen.data?.presets ?? []
  const run = () => {
    const body: ScShockRequest = {
      target_type: target,
      security_id: target === 'company' ? id : null,
      iso2: target === 'country' ? country : null,
      magnitude: mag / 100,
      damping: damping / 100,
      hops: Number(hops),
      demand_channel: true,
      supply_channel: supply,
      supply_factor: 0.5,
      default_share: 0.05,
      include_statistical: stat,
      include_home_country: homeToo,
      macro: macro ? { scenario_id: macro } : null,
      top_n: 30,
    }
    sh.mutate(body)
  }
  return (
    <Panel
      title={<span className="inline-flex items-center gap-2"><Zap className="size-4 text-accent" aria-hidden />Shock scenario</span>}
      subtitle={<span className="inline-flex flex-wrap items-center gap-2"><Badge tone="warn">Model-based</Badge><span className="text-2xs text-muted">{net.labels.shock}</span></span>}
    >
      <div className="flex flex-wrap items-end gap-3 text-xs">
        <label className="space-y-1">
          <span className="block text-2xs text-muted">Shock</span>
          <Segmented label="Shock target" size="xs" value={target} onChange={setTarget} options={[{ value: 'company', label: 'This company' }, { value: 'country', label: 'A country' }]} />
        </label>
        {target === 'country' && (
          <label className="space-y-1">
            <span className="block text-2xs text-muted">Country</span>
            <Select label="Shocked country" value={country} onChange={setIso} options={(countries.length ? countries : ['US']).map((c) => ({ value: c, label: `${c} · ${countryName(c)}` }))} className="w-48" />
          </label>
        )}
        <label className="w-48 space-y-1">
          <span className="block text-2xs text-muted">Revenue shock {mag > 0 ? '+' : ''}{mag}%</span>
          <Slider label="Revenue shock percent" min={-50} max={50} step={5} value={[mag]} onValueChange={(v) => setMag(v[0])} />
        </label>
        <label className="w-36 space-y-1">
          <span className="block text-2xs text-muted">Damping {damping}%</span>
          <Slider label="Damping per hop" min={0} max={100} step={10} value={[damping]} onValueChange={(v) => setDamping(v[0])} />
        </label>
        <label className="space-y-1">
          <span className="block text-2xs text-muted">Hops</span>
          <Segmented label="Hops" size="xs" value={hops} onChange={setHops} options={[{ value: '1', label: '1' }, { value: '2', label: '2' }, { value: '3', label: '3' }]} />
        </label>
        <label className="flex items-center gap-1.5 pb-1"><Checkbox label="Supply channel" checked={supply} onCheckedChange={(v) => setSupply(v === true)} /> Supply channel</label>
        <label className="flex items-center gap-1.5 pb-1"><Checkbox label="Statistical neighbours" checked={stat} onCheckedChange={(v) => setStat(v === true)} /> Statistical</label>
        {target === 'country' && (
          <label className="flex items-center gap-1.5 pb-1" title="Also seed companies without a geographic revenue disclosure (100% home country assumed)"><Checkbox label="Include home-country-only companies" checked={homeToo} onCheckedChange={(v) => setHomeToo(v === true)} /> Home-country-only</label>
        )}
        <label className="space-y-1">
          <span className="block text-2xs text-muted">W4 macro overlay</span>
          <Select label="Macro scenario overlay" value={macro} onChange={setMacro} options={[{ value: '', label: 'None' }, ...presets.map((p) => ({ value: p.scenario_id ?? '', label: p.name }))]} className="w-56" />
        </label>
        <Button size="sm" onClick={run} disabled={sh.isPending || (target === 'country' && !country)}>{sh.isPending ? 'Running…' : 'Run shock'}</Button>
      </div>
      {sh.isError && <ErrorState error={sh.error} className="mt-3" />}
      {sh.data && <ShockResult r={sh.data} />}
      {!sh.data && !sh.isPending && <p className="mt-3 text-2xs text-muted">Propagates a revenue shock to suppliers (demand channel: share of their revenue) and customers (supply channel), damped per hop. Results are estimates of revenue impact; the optional macro overlay adds the W4 factor-beta equity estimate.</p>}
    </Panel>
  )
}

function ShockResult({ r }: { r: ScShockResult }) {
  const navigate = useNavigate()
  const rows = shockRows(r.impacts)
  return (
    <div className="mt-3 space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-2xs text-muted">
        <span>Seed: <b className="text-fg">{r.seed.name}</b> ({r.seed.type}{r.seed.type === 'country' ? `, ${fmtInt(r.seed.n_seeds)} exposed companies` : ''}) · {fmtInt(r.n_affected)} nodes affected</span>
        {r.macro && <Badge tone="info">{r.macro.name} · W4 betas as of {r.macro.as_of ?? DASH}</Badge>}
        {r.notes.map((n) => <span key={n} className="text-faint">{n}</span>)}
      </div>
      {rows.length ? (
        <div className="max-h-[360px] overflow-auto">
          <table className="w-full text-xs" aria-label="Shock propagation results">
            <thead className="sticky top-0 bg-surface text-left text-muted">
              <tr className="border-b border-border">
                {['Company', 'Revenue impact*', 'Hop', 'Channel', 'Path', 'Macro channel*'].map((h, i) => <th key={h} className={`px-2 py-1.5 font-medium ${i === 1 || i === 2 || i === 5 ? 'text-right' : ''}`}>{h}</th>)}
              </tr>
            </thead>
            <tbody>
              {rows.map((x) => (
                <tr key={x.key} className="border-b border-border/60">
                  <td className="max-w-[200px] truncate px-2 py-1">
                    {x.security_id ? <button type="button" className="text-left font-medium text-accent-text hover:underline" onClick={() => navigate(securityRoute(x.security_id as string))}>{x.ticker ?? x.name}</button> : <span>{x.name}</span>}
                    <span className="ml-1 text-2xs text-muted">{x.country ?? ''}</span>
                  </td>
                  <td className={`num px-2 py-1 text-right ${x.revenue_impact < 0 ? 'text-down' : 'text-up'}`}>{fmtPct(x.revenue_impact, 2, true)}</td>
                  <td className="num px-2 py-1 text-right">{x.first_hop}</td>
                  <td className="px-2 py-1"><Badge tone={x.channel === 'demand' ? 'accent' : x.channel === 'supply' ? 'info' : 'neutral'}>{x.channel}</Badge></td>
                  <td className="max-w-[280px] truncate px-2 py-1 text-2xs text-muted" title={pathText(x)}>{pathText(x)}</td>
                  <td className="num px-2 py-1 text-right">{x.macro_impact != null ? fmtPct(x.macro_impact, 1, true) : DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <EmptyState title="No propagation" message="The shocked node has no disclosed links (or no exposure to the country)." className="py-6" />
      )}
      {r.statistical.length > 0 && (
        <p className="text-2xs text-muted">
          Statistical co-movement (not causal): {r.statistical.slice(0, 8).map((s) => `${s.ticker ?? s.name} ${fmtPct(s.rho_impact, 1, true)}`).join(' · ')}
        </p>
      )}
      <details className="text-2xs text-faint">
        <summary className="cursor-pointer">Method</summary>
        <ul className="mt-1 list-disc space-y-0.5 pl-4">{r.method.map((m) => <li key={m}>{m}</li>)}</ul>
      </details>
      <p className="text-2xs text-faint">* {r.model_label}</p>
    </div>
  )
}
