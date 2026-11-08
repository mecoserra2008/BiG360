import { cn } from '@/lib/utils'

/** Compact semicircular score gauge (0–100) in SVG; crisp at header sizes, themed via CSS vars. */
export function ScoreGauge({ value, label = 'Composite', size = 92, className }: { value: number; label?: string; size?: number; className?: string }) {
  const known = Number.isFinite(value)
  const v = known ? Math.max(0, Math.min(100, value)) : 0
  const r = size / 2 - 7
  const cx = size / 2
  const cy = size / 2 + 2
  const arc = (from: number, to: number) => {
    const a0 = Math.PI * (1 - from / 100)
    const a1 = Math.PI * (1 - to / 100)
    const x0 = cx + r * Math.cos(a0)
    const y0 = cy - r * Math.sin(a0)
    const x1 = cx + r * Math.cos(a1)
    const y1 = cy - r * Math.sin(a1)
    return `M${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 0 1 ${x1.toFixed(2)},${y1.toFixed(2)}`
  }
  const tone = v >= 67 ? 'var(--up)' : v <= 33 ? 'var(--down)' : 'var(--accent)'
  return (
    <div className={cn('relative inline-flex flex-col items-center', className)} style={{ width: size }} role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v)} aria-label={`${label} score`}>
      <svg width={size} height={size / 2 + 8} aria-hidden>
        <path d={arc(0, 100)} stroke="var(--surface-3)" strokeWidth={6} fill="none" strokeLinecap="butt" />
        {[33.3, 66.7].map((t) => {
          const a = Math.PI * (1 - t / 100)
          return <line key={t} x1={cx + (r - 5) * Math.cos(a)} y1={cy - (r - 5) * Math.sin(a)} x2={cx + (r + 5) * Math.cos(a)} y2={cy - (r + 5) * Math.sin(a)} stroke="var(--surface)" strokeWidth={1.5} />
        })}
        {v > 0 && <path d={arc(0, v)} stroke={tone} strokeWidth={6} fill="none" strokeLinecap="butt" />}
      </svg>
      <div className="absolute inset-x-0 text-center" style={{ top: size / 2 - 16 }}>
        <div className="num text-xl leading-6 font-semibold text-fg">{known ? Math.round(v) : '—'}</div>
      </div>
      <div className="label-caps -mt-0.5">{label}</div>
    </div>
  )
}
