"""Data Ops (O1): schedule profiles, coverage registry, gap detector, gap filler, housekeeping and the supervisor.

Modules
    winsys        ctypes helpers (memory, power, process tree, priority, keep-awake, wake timer, metered network)
    paths         config / state locations (config/*.yaml, data/state/ops/*)
    schedule      schedule profiles (config/schedule.yaml) -> APScheduler triggers, catch-up rule, refresh chain
    coverage      contracts/coverage.yaml loader (datasets, calendars, SLAs, sources, fill specs, campaigns)
    calendars     expected sessions / release dates (exchange_calendars, CFTC, 13F deadlines, FINRA)
    gaps          gap detector (job data_gaps_detect -> ops/data_gaps)
    budgets       per-source rate budgets for the filler (sliding window, persisted)
    governor      resource governor (RAM cap, free RAM, battery, metered network, disk, DAG windows)
    tasks         filler task runners (subprocess campaigns, runner jobs, gap fills)
    filler        long-running low-priority gap filler (python -m app.ops.filler)
    housekeeping  log rotation, disk guard, serving snapshot clean-up
    health        ops alerts + scheduler / data-as-of summary for /system/status and /system/data-health
    supervisor    starts + monitors API, worker and filler (python -m app.ops.supervisor)
"""
