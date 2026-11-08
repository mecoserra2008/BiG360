import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { XaAssetDetail, XaClass, XaCockpit, XaCorrelation, XaCurve, XaCurveList, XaRisk, XaWindow } from './types'

function live(): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/cross-asset`, 'operation CrossAsset needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

export const crossAssetApi = {
  async cockpit(assetClass?: XaClass, securityId?: string): Promise<XaCockpit> {
    live()
    return http.get('/cross-asset/cockpit', { asset_class: assetClass, security_id: securityId })
  },
  async asset(id: string, from: string | null): Promise<XaAssetDetail> {
    live()
    return http.get(`/cross-asset/assets/${encodeURIComponent(id)}`, { from: from ?? undefined })
  },
  async curves(): Promise<XaCurveList> {
    live()
    return http.get('/cross-asset/curves')
  },
  async curve(root: string, from: string | null): Promise<XaCurve> {
    live()
    return http.get(`/cross-asset/curves/${encodeURIComponent(root)}`, { from: from ?? undefined })
  },
  async correlation(window: XaWindow, from: string | null): Promise<XaCorrelation> {
    live()
    return http.get('/cross-asset/correlation', { window, from: from ?? undefined })
  },
  async risk(from: string | null): Promise<XaRisk> {
    live()
    return http.get('/cross-asset/risk', { from: from ?? undefined })
  },
}
