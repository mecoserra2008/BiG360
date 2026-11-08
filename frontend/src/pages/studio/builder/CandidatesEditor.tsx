import type { LeaderboardCandidate } from '@/api/studio/types'
import { Checkbox, Input } from '@/components/ui/inputs'
import { cn } from '@/lib/utils'

type Method = LeaderboardCandidate['method']

/** Leaderboard candidate methods (POST /studio/leaderboard). */
const CANDIDATES: { method: Method; label: string; hint: string }[] = [
  { method: 'mean', label: 'Historical mean', hint: 'benchmark' },
  { method: 'ols', label: 'OLS', hint: 'linear' },
  { method: 'logit', label: 'Logit', hint: 'classification' },
  { method: 'ridge', label: 'Ridge', hint: 'L2, alpha by inner CV' },
  { method: 'lasso', label: 'Lasso', hint: 'L1, alpha by inner CV' },
  { method: 'elasticnet', label: 'ElasticNet', hint: 'L1 + L2' },
  { method: 'rf', label: 'Random forest', hint: 'trees' },
  { method: 'hgb', label: 'Gradient boosting', hint: 'HistGradientBoosting' },
  { method: 'xgb', label: 'XGBoost', hint: 'trees' },
  { method: 'arima', label: 'ARIMA(X)', hint: 'time series only' },
]

/** Candidate checkboxes; existing entries (custom names / params from a preset) are kept as they are. */
export function CandidatesEditor({ value, onChange }: { value: LeaderboardCandidate[]; onChange: (v: LeaderboardCandidate[]) => void }) {
  const byMethod = new Map(value.map((c) => [c.method, c]))
  return (
    <div className="space-y-1 rounded-sm border border-border p-2">
      <div className="grid grid-cols-2 gap-x-3 gap-y-1">
        {CANDIDATES.map((c) => {
          const cur = byMethod.get(c.method)
          return (
            <div key={c.method} className={cn('flex min-w-0 items-center gap-1.5 text-xs', cur ? 'text-fg' : 'text-muted')}>
              <Checkbox
                label={`Candidate ${c.label}`}
                checked={!!cur}
                onCheckedChange={(on) =>
                  onChange(on ? [...value, { name: c.label, method: c.method, ...(c.method === 'arima' ? { params: { order: [1, 0, 1] } } : {}) }] : value.filter((x) => x.method !== c.method))
                }
              />
              {cur ? (
                <Input aria-label={`Name of the ${c.label} candidate`} className="h-6 min-w-0 flex-1 px-1.5 text-xs" value={cur.name} onChange={(e) => onChange(value.map((x) => (x === cur ? { ...x, name: e.target.value } : x)))} />
              ) : (
                <span className="truncate" title={c.hint}>
                  {c.label}
                </span>
              )}
            </div>
          )
        })}
      </div>
      <p className="text-2xs text-muted">{value.length} selected · every candidate is scored on the same cross-validation splits; Diebold–Mariano tests compare each with the best.</p>
    </div>
  )
}
