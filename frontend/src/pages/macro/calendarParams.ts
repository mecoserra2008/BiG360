/** `ECO <cc>` opens /macro/calendar?country=<cc> (guide 14): the calendar reads its country filter from the URL. */
const ALIAS: Record<string, string> = { UK: 'GB', EU: 'EA', EZ: 'EA', EMU: 'EA' }

export function calendarCountryParam(v: string | null | undefined): string {
  const c = (v ?? '').trim().toUpperCase()
  if (!c) return ''
  return ALIAS[c] ?? c
}
