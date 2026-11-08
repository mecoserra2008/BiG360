import { describe, expect, it } from 'vitest'
import { parseCommand, routeFor } from '@/lib/mnemonics'
import { calendarCountryParam } from '../calendarParams'

describe('ECO <cc> filters the calendar (guide 14)', () => {
  it('reads the country from the URL the mnemonic builds', () => {
    const path = routeFor(parseCommand('ECO JP'), { hasRoute: () => false })?.path ?? ''
    expect(path).toBe('/macro/calendar?country=JP')
    expect(calendarCountryParam(new URL(path, 'http://x').searchParams.get('country'))).toBe('JP')
    expect(calendarCountryParam('jp')).toBe('JP')
    expect(calendarCountryParam('uk')).toBe('GB')
    expect(calendarCountryParam('EU')).toBe('EA')
    expect(calendarCountryParam(null)).toBe('')
  })
})
