import { DASH, fmtCompact, fmtNum } from '@/lib/format'

export const fmtEps = (v: number | null | undefined): string => (typeof v === 'number' && Number.isFinite(v) ? fmtNum(v, 2) : DASH)
export const fmtRev = (v: number | null | undefined): string => (typeof v === 'number' && Number.isFinite(v) ? fmtCompact(v, 2) : DASH)
