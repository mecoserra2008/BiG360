import { resolveId } from '../client'
import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { CfCoverage, CfEnvelope, CfPairBand, CfPeerGroups, CfTouch, PairQuery, PeerGroupQuery } from './types'

/** Conformal data comes from the live API (model outputs); the mock shows "pending". */
function live(path: string): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}${path}`, 'operation conformal needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

const enc = encodeURIComponent

export const conformalApi = {
  async envelope(id: string): Promise<CfEnvelope> {
    live('/conformal/envelope/{security_id}')
    const sid = await resolveId(id)
    return http.get<CfEnvelope>(`/conformal/envelope/${enc(sid)}`)
  },
  async touch(id: string, horizon: number, levels?: number[]): Promise<CfTouch> {
    live('/conformal/touch')
    const sid = await resolveId(id)
    return http.get<CfTouch>('/conformal/touch', {
      security_id: sid,
      horizon,
      levels: levels && levels.length ? levels.map((x) => String(+x.toPrecision(10))).join(',') : undefined,
    })
  },
  async peerGroups(q: PeerGroupQuery): Promise<CfPeerGroups> {
    live('/conformal/peer-groups')
    const sid = q.securityId ? await resolveId(q.securityId) : undefined
    return http.get<CfPeerGroups>('/conformal/peer-groups', {
      security_id: sid,
      group_id: q.groupId,
      region: q.region,
      sector_id: q.sectorId,
      subsector_id: q.subsectorId,
      kind: q.kind,
      anomalies_only: q.anomaliesOnly ? 'true' : undefined,
      limit: q.limit,
    })
  },
  async pairBand(q: PairQuery): Promise<CfPairBand> {
    live('/conformal/pair-band')
    return http.get<CfPairBand>('/conformal/pair-band', {
      group_id: q.groupId,
      i: q.weights ? undefined : q.i,
      j: q.weights ? undefined : q.j,
      beta: q.weights || q.beta === null || q.beta === undefined || !Number.isFinite(q.beta) ? undefined : q.beta,
      weights: q.weights,
    })
  },
  async coverage(includeDoc = false): Promise<CfCoverage> {
    live('/conformal/coverage')
    return http.get<CfCoverage>('/conformal/coverage', { include_doc: includeDoc ? 'true' : undefined })
  },
}
