"""Supply-chain & revenue-geography map (roadmap v2 item #9; definitions docs/SUPPLY_CHAIN.md).

Modules:
  neighbours  STATISTICAL economic neighbours from weekly return correlations / partial correlations (labelled
              statistical, not disclosed) -> company_neighbours (job supply_chain_neighbours, weekly)
  network     disclosed supply-chain edges (company_links customer + supplier, PIT) -> sc_edges; per-issuer
              centrality / concentration / geography metrics -> sc_metrics; direct + second-order (customers')
              country / sector exposure -> sc_exposure; model-based exposure-weighted country risk -> country_risk
              (job supply_chain_network, nightly)
  contagion   transparent damped propagation of a company / country shock over the network (+ W4 macro channel
              in the API); watchlist / portfolio exposure aggregation. Model-based.
Extraction lives in app.linkage (textunits, textrules, customers, georev, esef_geo).
"""
