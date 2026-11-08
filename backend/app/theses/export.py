"""Markdown export of a thesis and the post-mortem template written on close (roadmap #8). No PDF dependency exists in
the backend, so PDF output is the browser print stylesheet of the thesis page."""

from __future__ import annotations

from typing import Any

DIR = {"long": "Long", "short": "Short", "neutral": "Neutral / watch"}
STATUS = {"on_track": "on track", "at_risk": "at risk", "invalidated": "invalidated", "unknown": "unknown"}


def _pct(v: Any) -> str:
    return "n/a" if v is None else f"{v:+.1%}"


def _num(v: Any) -> str:
    return "n/a" if v is None else f"{v:,.4g}"


def _kpi_line(k: dict[str, Any], vals: dict[str, dict[str, Any]] | None = None) -> str:
    op = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "between": "between"}.get(k.get("op", ""), k.get("op", ""))
    thr = f"{_num(k.get('threshold'))}" + (f" and {_num(k.get('threshold_hi'))}" if k.get("op") == "between" else "")
    what = k.get("expr") if k.get("kind") == "expr" else k.get("ref")
    v = (vals or {}).get(k["kpi_id"])
    now = "" if v is None else f" | now {_num(v.get('value'))} ({'ok' if v.get('ok') else 'breached' if v.get('ok') is False else 'no data'})"
    return f"- **{k.get('label') or k['kpi_id']}** ({k.get('role', 'confirm')}): `{what}` {op} {thr}{now}"


def post_mortem_template(t: dict[str, Any], perf: dict[str, Any] | None, first: dict[str, Any] | None,
                         last: dict[str, Any] | None) -> str:
    p = perf or {}
    kv0 = {k["kpi_id"]: k for k in (first or {}).get("kpi_values", [])}
    kv1 = {k["kpi_id"]: k for k in (last or {}).get("kpi_values", [])}
    lines = [f"## Post-mortem: {t['title']}", "",
             f"- Direction: {DIR.get(t['direction'], t['direction'])} | horizon {t['horizon']} | conviction {t['conviction']}/5",
             f"- Held {p.get('start_date', 'n/a')} -> {p.get('end_date', 'n/a')} ({p.get('days', 'n/a')} days)",
             f"- Thesis return {_pct(p.get('thesis_return'))} | vs benchmark {_pct(p.get('excess_vs_benchmark'))} | "
             f"vs sector {_pct(p.get('excess_vs_sector'))} | max drawdown {_pct(p.get('max_drawdown'))} ({p.get('basis', 'local')})",
             f"- Close reason: {t.get('close_reason') or 'n/a'} | outcome: {t.get('outcome') or 'n/a'}", ""]
    if kv0 or kv1:
        lines.append("### KPIs: at inception -> at close")
        for kid in dict.fromkeys([*kv0, *kv1]):
            a, b = kv0.get(kid, {}), kv1.get(kid, {})
            lines.append(f"- {b.get('label') or a.get('label') or kid}: {_num(a.get('value'))} -> {_num(b.get('value'))}")
        lines.append("")
    lines += ["### What was the thesis?", "", "_Restate the original thesis in one or two sentences._", "",
              "### What happened?", "", "_Which drivers and catalysts played out, which did not?_", "",
              "### Right for the right reasons?", "", "_Separate skill from luck: was the outcome caused by the thesis "
              "drivers or by something else (market beta, macro, an unrelated event)?_", "",
              "### Process", "", "_Entry, sizing, stop / target discipline, review cadence: what would you repeat or "
              "change?_", "", "### Lessons", "", "- ", ""]
    return "\n".join(lines)


def to_markdown(t: dict[str, Any], journal: list[dict[str, Any]], reviews: list[dict[str, Any]],
                snapshots: list[dict[str, Any]], perf: dict[str, Any] | None, status: dict[str, Any] | None,
                alerts: list[dict[str, Any]] | None = None, events: list[dict[str, Any]] | None = None) -> str:
    p = perf or {}
    st = status or {}
    vals = {k["kpi_id"]: k for k in st.get("kpis", [])}
    L = [f"# {t['title']}", "",
         f"*{DIR.get(t['direction'], t['direction'])} - {t.get('subject_label') or t['subject_id']} "
         f"({t['subject_type'].replace('_', ' ')}) - horizon {t['horizon']} - conviction {t['conviction']}/5 - "
         f"status {t['status']} - automatic status {STATUS.get(st.get('auto_status') or t.get('auto_status') or 'unknown')}*",
         "", f"Created {t['created_at'][:10]}" + (f", active since {t['inception_date']}" if t.get("inception_date") else "")
         + (f", closed {t['closed_at'][:10]}" if t.get("closed_at") else "") + f". Thesis id `{t['thesis_id']}`.", ""]
    if t.get("thesis_md"):
        L += ["## Thesis", "", t["thesis_md"].strip(), ""]
    if t.get("key_drivers"):
        L += ["## Key drivers", "", *[f"- {d}" for d in t["key_drivers"]], ""]
    L += ["## Price plan", "",
          f"- Instrument: `{t.get('price_id') or 'none'}` ({t.get('price_currency') or 'n/a'})",
          f"- Entry band: {_num(t.get('entry_lo'))} - {_num(t.get('entry_hi'))} | target {_num(t.get('target'))} | "
          f"stop {_num(t.get('stop'))}",
          f"- Inception price: {_num(t.get('inception_price'))} | last: {_num(st.get('price'))}"
          + (f" ({st.get('price_date')})" if st.get("price_date") else ""), ""]
    if t.get("catalysts"):
        L += ["## Catalysts", "", *[f"- {c.get('date') or 'undated'}: {c.get('label')} ({c.get('kind', 'custom')})"
                                    + (" - done" if c.get("done") else "") for c in t["catalysts"]], ""]
    if t.get("kpis"):
        L += ["## KPIs to watch", "", *[_kpi_line(k, vals) for k in t["kpis"]], ""]
    if t.get("invalidation"):
        L += ["## Invalidation conditions", "", *[f"- {c.get('label') or c.get('kind')}" for c in t["invalidation"]], ""]
    if st.get("reasons"):
        L += ["## Automatic status", "", *[f"- [{r['level']}] {r['message']}" for r in st["reasons"]], ""]
    if p:
        L += ["## Performance since inception", "",
              f"- {p.get('start_date')} -> {p.get('end_date')} ({p.get('days')} days, basis {p.get('basis')})",
              f"- Thesis return {_pct(p.get('thesis_return'))}; vs benchmark `{(p.get('benchmark') or {}).get('id')}` "
              f"{_pct(p.get('excess_vs_benchmark'))}; vs sector `{(p.get('sector') or {}).get('id')}` "
              f"{_pct(p.get('excess_vs_sector'))}; max drawdown {_pct(p.get('max_drawdown'))}", ""]
    links = [("W4 scenario", t.get("scenario_id")), ("Studio study", t.get("study_id")), ("Studio run", t.get("studio_run_id")),
             ("Backtest strategy", t.get("backtest_strategy_id")), ("Backtest run", t.get("backtest_run_id"))]
    if any(v for _, v in links):
        L += ["## Linked analysis", "", *[f"- {k}: `{v}`" for k, v in links if v], ""]
    if alerts:
        L += ["## Linked alert rules", "", *[f"- {a['rule_type']}" + (f" {a['params'].get('band')} {a['params'].get('level')}"
                                                                    if a["rule_type"] == "price_cross" else "")
                                            for a in alerts], ""]
    if events:
        L += ["## Triggered alerts", "", *[f"- {e['as_of_date']}: {e['message']}" for e in events[:50]], ""]
    if reviews:
        L += ["## Reviews", "", *[f"- {r['due_date']} {r['status']}" + (f": {r['decision']}" if r.get("decision") else "")
                                  + (f" (conviction -> {r['conviction_after']})" if r.get("conviction_after") else "")
                                  + (f" - {r['notes_md'].strip()}" if r.get("notes_md") else "") for r in reviews], ""]
    if snapshots:
        L += ["## Snapshots", "", "| Taken | Kind | Price | Score | Status |", "|---|---|---|---|---|",
              *[f"| {s['taken_at'][:10]} | {s['kind']} | {_num(s.get('price'))} | {_num(s.get('composite_score'))} | "
                f"{s.get('auto_status') or ''} |" for s in snapshots], ""]
    if journal:
        L += ["## Journal", ""]
        for e in sorted(journal, key=lambda x: (x["entry_date"], x["created_at"])):
            L += [f"### {e['entry_date']} - {e['kind'].replace('_', ' ')}", "", e["body_md"].strip(), ""]
            for lk in e.get("links") or []:
                L.append(f"- {lk.get('kind')}: {lk.get('label') or lk.get('ref')}" + (f" <{lk['url']}>" if lk.get("url") else ""))
            L.append("")
    if t.get("post_mortem_md"):
        L += [t["post_mortem_md"].strip(), ""]
    L += ["---", "_Exported from BiG Equity 360. Automatic status, KPI medians and z-scores are model-based; prices "
          "from the serving snapshot._", ""]
    return "\n".join(L)
