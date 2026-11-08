import type { PriceBar } from '../types'
import { BY_ID } from './universe'
import { MOCK_AS_OF, Rng, businessDays, round } from './rng'

const cache = new Map<string, PriceBar[]>()

/**
 * Deterministic daily OHLCV path that ends exactly at the universe price.
 * Regime-switching drift + vol clustering so technical levels look plausible.
 */
const FULL = 760

export function priceBars(id: string, count = FULL): PriceBar[] {
  const full = fullPath(id)
  return count >= full.length ? full : full.slice(full.length - count)
}

function fullPath(id: string): PriceBar[] {
  const count = FULL
  const key = id
  const hit = cache.get(key)
  if (hit) return hit
  const e = BY_ID.get(id)
  const last = e?.price ?? 100
  const r = new Rng(`px:${id}`)
  const dates = businessDays(MOCK_AS_OF, count)
  const annVol = 0.14 + r.next() * 0.16
  const target = annVol / Math.sqrt(252)
  let vol = target
  let drift = 0
  const rets: number[] = []
  for (let i = 0; i < count; i++) {
    if (i % 60 === 0) drift = r.normal(0.0003, 0.00035)
    // GARCH(1,1)-like clustering that mean-reverts to the target daily vol
    const z = r.normal()
    vol = Math.sqrt(0.9 * vol * vol + 0.1 * target * target * z * z)
    const shock = r.chance(0.006) ? r.normal(0, target * 3) : 0
    rets.push(drift + r.normal(0, Math.max(0.003, vol)) + shock)
  }
  // build path then rescale to end at `last`
  const closes: number[] = []
  let p = 1
  for (const x of rets) {
    p *= Math.exp(x)
    closes.push(p)
  }
  const scale = last / closes[closes.length - 1]
  const dec = last > 5000 ? 0 : 2
  const avgVol = Math.max(2e4, ((e?.marketCapUsd ?? 5e9) / (e?.fxToUsd ?? 1) / last) * 0.004)
  const bars: PriceBar[] = []
  let prevClose = closes[0] * scale
  for (let i = 0; i < count; i++) {
    const c = closes[i] * scale
    const o = prevClose * (1 + r.normal(0, vol * 0.35))
    const range = Math.abs(r.normal(0, vol * 0.9)) * c
    const h = Math.max(o, c) + range * r.next()
    const l = Math.min(o, c) - range * r.next()
    bars.push({
      time: dates[i],
      open: round(o, dec),
      high: round(h, dec),
      low: round(Math.max(l, 0.01), dec),
      close: round(c, dec),
      volume: Math.round(avgVol * Math.exp(r.normal(0, 0.45)) * (1 + Math.abs(rets[i]) * 25)),
    })
    prevClose = c
  }
  bars[bars.length - 1].close = last
  cache.set(key, bars)
  return bars
}

export function closesWeekly(id: string, weeks = 104): { date: string; close: number }[] {
  const bars = priceBars(id)
  const out: { date: string; close: number }[] = []
  for (let i = bars.length - 1; i >= 0 && out.length < weeks; i -= 5) out.push({ date: bars[i].time, close: bars[i].close })
  return out.reverse()
}
