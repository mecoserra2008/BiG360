"""Linkage data (plan A1-A7): geography, trade, input-output, banking, company geography / customers, spatial weights.

Modules:
  common     paths, state, http clients, ISO code helpers
  geo        A1  Natural Earth admin-0 -> geo_countries (+ contiguity / distance inputs)
  trade      A2  IMF IMTS (DOTS successor) + UN Comtrade preview -> trade_bilateral
  io         A3  OECD ICIO -> io_flows (country x sector flows, our sector taxonomy)
  banking    A4  BIS LBS / CBS -> bank_claims
  georev     A5  SEC Financial Statement Data Sets (segments) + IFRS dimensional facts -> company_geo_revenue
  customers  A6  XBRL MajorCustomers members + 10-K text rules + GDELT co-mentions -> company_links
  weights    A7  spatial_weights builder + get_W() helper API
  textunits  #9  compact link-candidate sentence store (sec_text corpus + latest 10-K / 20-F / 40-F, no HTML kept)
  textrules  #9  rule-based customer / supplier extraction (no LLM) with provenance
  esef_geo   #9  geographic revenue tables of tagged ESEF segment notes -> company_geo_revenue (esef_textblock)
  jobs       job entry points (contracts/jobs.yaml)
"""
