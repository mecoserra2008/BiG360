/**
 * Technicals v2 T5 (contract v1.33.0): GET /technicals/{security_id}/context - volatility, regime and earnings context
 * (range-width squeeze / expansion, realised / implied vol, ATR, ADX, daily / weekly trend alignment, earnings).
 */
import type { components } from '../generated/schema'

export type TechContext = components['schemas']['TechnicalsContext']
