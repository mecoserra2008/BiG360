import { useId } from 'react'
import { cn } from '@/lib/utils'

/**
 * Lightweight SVG sparkline for dense tables and tiles (no canvas instance per row).
 * Colour follows direction of first→last unless `tone` is forced.
 */
export function Sparkline({
  data,
  width = 72,
  height = 20,
  tone = 'auto',
  invert = false,
  area = true,
  className,
  ariaLabel,
}: {
  data: (number | null)[]
  width?: number
  height?: number
  tone?: 'auto' | 'neutral' | 'accent'
  invert?: boolean
  area?: boolean
  className?: string
  ariaLabel?: string
}) {
  const gid = useId()
  const pts = data.map((v, i) => [i, v] as const).filter((p): p is readonly [number, number] => typeof p[1] === 'number' && Number.isFinite(p[1]))
  if (pts.length < 2) return <svg width={width} height={height} className={className} aria-hidden />
  const ys = pts.map((p) => p[1])
  const min = Math.min(...ys)
  const max = Math.max(...ys)
  const span = max - min || 1
  const n = data.length - 1 || 1
  const x = (i: number) => 1 + (i / n) * (width - 2)
  const y = (v: number) => 1.5 + (1 - (v - min) / span) * (height - 3)
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join('')
  const first = ys[0]
  const last = ys[ys.length - 1]
  const dir = last === first ? 0 : (last > first) !== invert ? 1 : -1
  const color = tone === 'accent' ? 'var(--accent)' : tone === 'neutral' || dir === 0 ? 'var(--muted)' : dir > 0 ? 'var(--up)' : 'var(--down)'
  const lastPt = pts[pts.length - 1]
  return (
    <svg width={width} height={height} className={cn('overflow-visible', className)} role={ariaLabel ? 'img' : undefined} aria-label={ariaLabel} aria-hidden={ariaLabel ? undefined : true}>
      {area && (
        <>
          <defs>
            <linearGradient id={gid} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.22} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <path d={`${d}L${x(lastPt[0]).toFixed(1)},${height}L${x(pts[0][0]).toFixed(1)},${height}Z`} fill={`url(#${gid})`} />
        </>
      )}
      <path d={d} fill="none" stroke={color} strokeWidth={1.25} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={x(lastPt[0])} cy={y(lastPt[1])} r={1.75} fill={color} />
    </svg>
  )
}
