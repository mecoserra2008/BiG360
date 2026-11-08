import type { CountryCode, CurrencyCode, RegionCode, RegionFilter } from '@/api/types'

export const REGIONS: { code: RegionFilter; label: string; title: string }[] = [
  { code: 'ALL', label: 'All', title: 'All regions' },
  { code: 'US', label: 'US', title: 'United States' },
  { code: 'EU', label: 'EU', title: 'Continental Europe (incl. CH, Nordics)' },
  { code: 'UK', label: 'UK', title: 'United Kingdom' },
  { code: 'JP', label: 'JP', title: 'Japan' },
  { code: 'KR', label: 'KR', title: 'South Korea' },
  { code: 'CNHK', label: 'CN/HK', title: 'Mainland China & Hong Kong' },
  { code: 'LATAM', label: 'LATAM', title: 'Latin America' },
]

export const REGION_LABEL: Record<RegionCode, string> = {
  US: 'US',
  EU: 'EU',
  UK: 'UK',
  JP: 'JP',
  KR: 'KR',
  CNHK: 'CN/HK',
  LATAM: 'LATAM',
  OTHER: 'Other',
}

export interface CountryMeta {
  name: string
  region: RegionCode
  currency: CurrencyCode
}

export const COUNTRIES: Record<CountryCode, CountryMeta> = {
  US: { name: 'United States', region: 'US', currency: 'USD' },
  DE: { name: 'Germany', region: 'EU', currency: 'EUR' },
  FR: { name: 'France', region: 'EU', currency: 'EUR' },
  NL: { name: 'Netherlands', region: 'EU', currency: 'EUR' },
  ES: { name: 'Spain', region: 'EU', currency: 'EUR' },
  IT: { name: 'Italy', region: 'EU', currency: 'EUR' },
  PT: { name: 'Portugal', region: 'EU', currency: 'EUR' },
  BE: { name: 'Belgium', region: 'EU', currency: 'EUR' },
  FI: { name: 'Finland', region: 'EU', currency: 'EUR' },
  IE: { name: 'Ireland', region: 'EU', currency: 'EUR' },
  CH: { name: 'Switzerland', region: 'EU', currency: 'CHF' },
  DK: { name: 'Denmark', region: 'EU', currency: 'DKK' },
  SE: { name: 'Sweden', region: 'EU', currency: 'SEK' },
  NO: { name: 'Norway', region: 'EU', currency: 'NOK' },
  GB: { name: 'United Kingdom', region: 'UK', currency: 'GBP' },
  JP: { name: 'Japan', region: 'JP', currency: 'JPY' },
  KR: { name: 'South Korea', region: 'KR', currency: 'KRW' },
  CN: { name: 'China', region: 'CNHK', currency: 'CNY' },
  HK: { name: 'Hong Kong', region: 'CNHK', currency: 'HKD' },
  BR: { name: 'Brazil', region: 'LATAM', currency: 'BRL' },
  MX: { name: 'Mexico', region: 'LATAM', currency: 'MXN' },
  AR: { name: 'Argentina', region: 'LATAM', currency: 'USD' },
  CL: { name: 'Chile', region: 'LATAM', currency: 'USD' },
}

/** Units of USD per 1 unit of currency (static mock FX; the backend supplies point-in-time FX). */
export const FX_TO_USD: Record<CurrencyCode, number> = {
  USD: 1,
  EUR: 1.09,
  GBP: 1.3,
  CHF: 1.15,
  DKK: 0.146,
  SEK: 0.096,
  NOK: 0.093,
  JPY: 0.0068,
  KRW: 0.00073,
  CNY: 0.139,
  HKD: 0.128,
  BRL: 0.18,
  MXN: 0.052,
}

export function countryName(cc: CountryCode): string {
  return COUNTRIES[cc]?.name ?? cc
}

export function regionOf(cc: CountryCode): RegionCode {
  return COUNTRIES[cc]?.region ?? 'US'
}

export function matchesRegion(region: RegionCode, filter: RegionFilter): boolean {
  return filter === 'ALL' || region === filter
}
