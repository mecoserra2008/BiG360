/**
 * Supply-chain & revenue geography (contract v1.27.0, tag supply-chain): /companies/{id}/network, /companies/{id}/geography,
 * /supply-chain/country-risk, /supply-chain/shock, /supply-chain/portfolio-exposure. Payloads keep the contract shape;
 * ./adapters derives graph / map / table structures.
 */
import type { components } from '../generated/schema'

type S = components['schemas']

export type ScNetwork = S['CompanyNetwork']
export type ScNode = S['SupplyChainNode']
export type ScEdge = S['SupplyChainEdge']
export type ScMetrics = S['SupplyChainMetrics']
export type ScCoverage = S['SupplyChainCoverage']
export type ScLabels = S['SupplyChainLabels']
export type ScGeography = S['CompanyGeography']
export type ScGeoCountry = S['CompanyGeographyCountry']
export type ScGeoSector = S['CompanyGeographySector']
export type ScOverlay = S['CountryRiskOverlay']
export type ScCountryRisk = S['SupplyChainCountryRisk']
export type ScCountryRiskRow = S['SupplyChainCountryRiskRow']
export type ScShockRequest = S['SupplyChainShockRequest']
export type ScShockResult = S['SupplyChainShockResult']
export type ScShockImpact = S['SupplyChainShockImpact']
export type ScPortfolioRequest = S['PortfolioExposureRequest']
export type ScPortfolio = S['PortfolioExposure']
export type ScPortfolioCountry = S['PortfolioExposureCountry']

export type EdgeType = ScEdge['type']
export type ExposureBasis = 'direct' | 'second_order' | 'look_through'
export type MapOverlay = 'exposure' | 'risk_score' | 'event_pressure' | 'recession_prob' | 'p_escalation_30d'
export type NodeColorBy = 'score' | 'event_pressure' | 'country_risk'

export interface NetworkQuery {
  depth: 1 | 2
  includeStatistical: boolean
  statisticalMethod: 'return_corr' | 'partial_corr'
  minConfidence: number
}
