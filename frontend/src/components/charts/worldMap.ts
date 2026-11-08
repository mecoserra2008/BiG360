import type { FeatureCollection, Geometry } from 'geojson'
import { feature } from 'topojson-client'
import type { GeometryCollection, Topology } from 'topojson-specification'
import { echarts } from './EChart'

/** ISO 3166 numeric → alpha-2 for every country in the bundled 110m atlas (Kosovo has no numeric code: matched by name). */
const NUM_TO_A2: Record<number, string> = {
  4: 'AF', 8: 'AL', 10: 'AQ', 12: 'DZ', 24: 'AO', 31: 'AZ', 32: 'AR', 36: 'AU', 40: 'AT', 44: 'BS', 50: 'BD', 51: 'AM', 56: 'BE',
  64: 'BT', 68: 'BO', 70: 'BA', 72: 'BW', 76: 'BR', 84: 'BZ', 90: 'SB', 96: 'BN', 100: 'BG', 104: 'MM', 108: 'BI', 112: 'BY', 116:
  'KH', 120: 'CM', 124: 'CA', 140: 'CF', 144: 'LK', 148: 'TD', 152: 'CL', 156: 'CN', 158: 'TW', 170: 'CO', 178: 'CG', 180: 'CD',
  188: 'CR', 191: 'HR', 192: 'CU', 196: 'CY', 203: 'CZ', 204: 'BJ', 208: 'DK', 214: 'DO', 218: 'EC', 222: 'SV', 226: 'GQ', 231:
  'ET', 232: 'ER', 233: 'EE', 238: 'FK', 242: 'FJ', 246: 'FI', 250: 'FR', 260: 'TF', 262: 'DJ', 266: 'GA', 268: 'GE', 270: 'GM',
  275: 'PS', 276: 'DE', 288: 'GH', 300: 'GR', 304: 'GL', 320: 'GT', 324: 'GN', 328: 'GY', 332: 'HT', 340: 'HN', 348: 'HU', 352:
  'IS', 356: 'IN', 360: 'ID', 364: 'IR', 368: 'IQ', 372: 'IE', 376: 'IL', 380: 'IT', 384: 'CI', 388: 'JM', 392: 'JP', 398: 'KZ',
  400: 'JO', 404: 'KE', 408: 'KP', 410: 'KR', 414: 'KW', 417: 'KG', 418: 'LA', 422: 'LB', 426: 'LS', 428: 'LV', 430: 'LR', 434:
  'LY', 440: 'LT', 442: 'LU', 450: 'MG', 454: 'MW', 458: 'MY', 466: 'ML', 478: 'MR', 484: 'MX', 496: 'MN', 498: 'MD', 499: 'ME',
  504: 'MA', 508: 'MZ', 512: 'OM', 516: 'NA', 524: 'NP', 528: 'NL', 540: 'NC', 548: 'VU', 554: 'NZ', 558: 'NI', 562: 'NE', 566:
  'NG', 578: 'NO', 586: 'PK', 591: 'PA', 598: 'PG', 600: 'PY', 604: 'PE', 608: 'PH', 616: 'PL', 620: 'PT', 624: 'GW', 626: 'TL',
  630: 'PR', 634: 'QA', 642: 'RO', 643: 'RU', 646: 'RW', 682: 'SA', 686: 'SN', 688: 'RS', 694: 'SL', 703: 'SK', 704: 'VN', 705:
  'SI', 706: 'SO', 710: 'ZA', 716: 'ZW', 724: 'ES', 728: 'SS', 729: 'SD', 732: 'EH', 740: 'SR', 748: 'SZ', 752: 'SE', 756: 'CH',
  760: 'SY', 762: 'TJ', 764: 'TH', 768: 'TG', 780: 'TT', 784: 'AE', 788: 'TN', 792: 'TR', 795: 'TM', 800: 'UG', 804: 'UA', 807:
  'MK', 818: 'EG', 826: 'GB', 834: 'TZ', 840: 'US', 854: 'BF', 858: 'UY', 860: 'UZ', 862: 'VE', 887: 'YE', 894: 'ZM',
}
const NAME_TO_A2: Record<string, string> = { Kosovo: 'XK' }

let ready: Promise<void> | null = null
const centers: Record<string, [number, number]> = {}

/** [lon, lat] label point per alpha-2 (centre of the largest polygon's bounding box) — available after ensureWorldMap(). */
export function countryCenter(a2: string): [number, number] | null {
  return centers[a2] ?? null
}

/** Registers the bundled Natural Earth 110m world map (from world-atlas; no network). Feature names = ISO alpha-2 where known. */
export function ensureWorldMap(): Promise<void> {
  ready ??= import('world-atlas/countries-110m.json').then((mod) => {
    const topo = (mod as unknown as { default: Topology }).default ?? (mod as unknown as Topology)
    const fc = feature(topo, topo.objects.countries as GeometryCollection) as unknown as FeatureCollection<Geometry, { name: string }>
    for (const f of fc.features) {
      const a2 = NUM_TO_A2[Number(f.id)] ?? NAME_TO_A2[f.properties?.name ?? '']
      f.properties = { name: a2 ?? f.properties?.name ?? String(f.id) }
    }
    // drop Antarctica to save vertical space
    fc.features = fc.features.filter((f) => Number(f.id) !== 10)
    // Russia (643) and Fiji (242) cross the antimeridian; shift their western-hemisphere
    // vertices east so polygons don't smear across the whole map.
    for (const f of fc.features) {
      if (Number(f.id) !== 643 && Number(f.id) !== 242) continue
      const fix = (ring: number[][]) => ring.forEach((pt) => { if (pt[0] < 0) pt[0] += 360 })
      const g = f.geometry
      if (g.type === 'Polygon') g.coordinates.forEach(fix)
      else if (g.type === 'MultiPolygon') g.coordinates.forEach((poly) => poly.forEach(fix))
    }
    for (const f of fc.features) {
      const g = f.geometry
      const polys = g.type === 'Polygon' ? [g.coordinates] : g.type === 'MultiPolygon' ? g.coordinates : []
      let best: number[][] | null = null
      let bestArea = -1
      for (const poly of polys) {
        const ring = poly[0]
        let a = 0
        for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) a += (ring[j][0] + ring[i][0]) * (ring[j][1] - ring[i][1])
        if (Math.abs(a) > bestArea) {
          bestArea = Math.abs(a)
          best = ring
        }
      }
      if (best && f.properties?.name) {
        const xs = best.map((p) => p[0])
        const ys = best.map((p) => p[1])
        centers[f.properties.name] = [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2]
      }
    }
    echarts.registerMap('world', fc as unknown as Parameters<typeof echarts.registerMap>[1])
  })
  return ready
}
