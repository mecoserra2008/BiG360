/** Market instruments (FX / futures / indices / ETFs) have synthetic ids IDX:<yf> / ETF:<yf>; they get the Instruments page. */
export const isInstrumentId = (id: string): boolean => /^(IDX|ETF):/i.test(id) || /=(X|F)$/i.test(id)

export function securityRoute(id: string): string {
  if (!isInstrumentId(id)) return `/company/${encodeURIComponent(id)}`
  const sid = /^(IDX|ETF):/i.test(id) ? id : `IDX:${id}`
  return `/instruments/${encodeURIComponent(sid)}`
}
