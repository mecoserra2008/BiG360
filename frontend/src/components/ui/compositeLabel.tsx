import { useFactorModel } from '@/api/hooks'
import { compositeWording, type CompositeWordingOpts } from '@/lib/compositeLabel'
import { Badge } from './badge'

/**
 * Coverage v3 A8: "Descriptive" badge on every composite score (Company 360 header and score breakdown, Screener,
 * Sector Explorer, Peers). It reads "Validated signal" only for a profile whose recorded out-of-sample gate passed
 * (GET /meta/factors composite_validation) AND when the API confirms the composites shown lie inside the population
 * that gate tested (`validated`: Company 360 overview composite.validated, Screener composite_label). Views without
 * that confirmation (Peers, Sector Explorer: mixed regions / scopes) stay descriptive; `profileLevel` describes the
 * gate itself (System page); a re-weighted composite (`custom`) is always descriptive.
 */
export function CompositeLabel({ className, ...opts }: CompositeWordingOpts & { className?: string }) {
  const f = useFactorModel()
  const w = compositeWording(f.data?.compositeValidation, opts)
  return (
    <Badge tone={w.tone} title={w.title} className={className} data-testid="composite-label" data-signal={w.signalAllowed ? 'true' : 'false'}>
      {w.label}
    </Badge>
  )
}

/** The wording itself, for subtitles / tooltips that name the score in running text. */
export function useCompositeWording(opts: CompositeWordingOpts = {}) {
  const f = useFactorModel()
  return compositeWording(f.data?.compositeValidation, opts)
}
