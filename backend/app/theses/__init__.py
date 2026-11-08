"""Thesis tracker (roadmap v2 item #8): a decision log with accountability.

    store       app_state persistence: theses, thesis_journal, thesis_reviews, thesis_snapshots + thesis-tagged alerts
    rules       linked alert rules generated from a thesis (price bands, KPIs, score / grade, earnings, event shocks,
                CFTC / smart money, reviews, catalysts, status)
    evaluate    KPI values (formula engine over the serving snapshot), automatic status with reasons, thesis_* alert
                events, snapshots; nightly job `thesis_eval` (jobs.yaml)
    perf        performance since inception vs benchmark / sector (shared price reader + FX), hit-rate stats
    export      Markdown export and the post-mortem template
"""
