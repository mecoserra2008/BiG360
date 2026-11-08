"""Earnings & corporate-events calendar (roadmap v2 item #6).

Jobs (contracts/jobs.yaml):
* earnings_dates_fetch  (app.earnings.fetch)        -> earnings_announcements (Yahoo dates, JPX schedule)
* earnings_events       (app.earnings.events)       -> earnings_events, earnings_calendar, corporate_events
* earnings_reactions    (app.earnings.reactions)    -> earnings_reactions (CAR / BHAR, realised-vol baseline)
* earnings_expectations (app.earnings.expectations) -> earnings_expectations (model-based, not consensus)
* earnings_signals      (app.earnings.signals)      -> earnings_season, earnings_pead

Labels: provider dates = "provider estimate", cadence dates = "model-based projection", expectations / surprises =
"Model-based expectation (not consensus)".
"""
