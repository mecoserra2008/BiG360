"""W3.1 Policy rates, corridors, decisions, meetings and rate-cycle phase (official sources only).

Inputs (see banks.py): each bank's own daily official series (FRED for the Fed target range / IORB / ON RRP / EFFR,
ECB Data Portal, BoE IADB, BoC Valet, RBA F1, Riksbank SWEA, Norges Bank, BCB SGS, BanRep), the BIS central bank
policy-rate database (WS_CBPOL, daily; cross-check everywhere, daily series where the bank's own daily series is not
machine-readable without a key), the LPR from the National Interbank Funding Center (PBoC-authorised publisher),
official meeting calendars (app.macro.calendar_sources: FOMC, ECB, BoE, BoJ, BoC, RBA, Copom) and the BoE MPC
voting history workbook (official, vote splits since 1997).

Decisions (method ``decisions_v1``): change points of the daily policy-rate step series (|change| >= 1bp). A change
whose effective date falls within 0..10 days after an official meeting is attributed to that meeting
(``date_basis='official_meeting'``); a change with no meeting in that window while the meeting calendar covers the
date is ``scheduled=False`` (inter-meeting move, dated at the effective date); before the calendar starts (or for
banks without a machine-readable calendar) decisions are dated at the effective date (``date_basis='effective_date'``).
Past meetings without a change are ``hold`` decisions. Vote splits only where an official machine-readable source
exists (BoE MPC voting history).

Rate-cycle phase (method ``cycle_v1``, table cb_rate_cycle, one row per bank and business day): ``hiking`` / ``cutting``
when the most recent rate change within the trailing 183 days was a hike / cut; otherwise ``pausing`` (no change in
the last 183 days, including long holds).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

from .banks import BANKS, Bank
from .common import UTC, http_get, http_post, lake_series, now_utc, read_table_safe, to_float, update_state, write

log = get_logger(__name__)

START = date(1990, 1, 1)
CHANGE_EPS = 0.005          # percent; rates are published with <= 3 decimals (Fed mid = x.x25/x.x75)
ATTRIB_DAYS = 10            # change effective within 0..10 days after a meeting -> that meeting's decision
CYCLE_WINDOW_DAYS = 183
METHOD_DECISIONS = "decisions_v1"
METHOD_CYCLE = "cycle_v1"

Pairs = list[tuple[date, float]]

BIS_URL = "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.{codes}"
ECB_URL = "https://data-api.ecb.europa.eu/service/data/{flow}/{key}"
RIKSBANK_URL = "https://api.riksbank.se/swea/v1/Observations/{series}/{start}"
NORGES_URL = "https://data.norges-bank.no/api/data/{flow}/{key}"
VALET_URL = "https://www.bankofcanada.ca/valet/observations/{codes}/json"
LPR_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/LprHis"
BOE_VOTES_URL = "https://www.bankofengland.co.uk/-/media/boe/files/monetary-policy-summary-and-minutes/mpcvoting.xlsx"


# =============================================================================================== parsers (pure)

def parse_bis_csv(text: str) -> dict[str, Pairs]:
    out: dict[str, Pairs] = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        v = to_float(r.get("OBS_VALUE"))
        try:
            d = date.fromisoformat(r.get("TIME_PERIOD", "")[:10])
        except ValueError:
            continue
        if v is not None:
            out.setdefault(r.get("REF_AREA", ""), []).append((d, v))
    return {k: sorted(v) for k, v in out.items()}


def parse_sdmx_csv_simple(text: str, sep: str = ",") -> Pairs:
    """ECB csvdata / Norges Bank csv: TIME_PERIOD + OBS_VALUE columns (daily or business frequency)."""
    out: Pairs = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿")), delimiter=sep):
        v = to_float(r.get("OBS_VALUE"))
        tp = (r.get("TIME_PERIOD") or "").strip()
        try:
            d = date.fromisoformat(tp[:10])
        except ValueError:
            continue
        if v is not None:
            out.append((d, v))
    return sorted(out)


def parse_riksbank(j: Any) -> Pairs:
    out: Pairs = []
    for o in j or []:
        v = to_float(o.get("value"))
        try:
            d = date.fromisoformat(str(o.get("date"))[:10])
        except ValueError:
            continue
        if v is not None:
            out.append((d, v))
    return sorted(out)


def parse_valet(j: dict[str, Any]) -> dict[str, Pairs]:
    out: dict[str, Pairs] = {}
    for o in j.get("observations", []) or []:
        try:
            d = date.fromisoformat(o["d"][:10])
        except (KeyError, ValueError):
            continue
        for k, v in o.items():
            if k != "d" and isinstance(v, dict):
                fv = to_float(v.get("v"))
                if fv is not None:
                    out.setdefault(k, []).append((d, fv))
    return {k: sorted(v) for k, v in out.items()}


def parse_lpr(j: dict[str, Any]) -> dict[str, Pairs]:
    out: dict[str, Pairs] = {}
    for rec in (j or {}).get("records") or []:
        try:
            d = date.fromisoformat(str(rec.get("showDateCN"))[:10])
        except ValueError:
            continue
        for k in ("1Y", "5Y"):
            v = to_float(rec.get(k))
            if v is not None:
                out.setdefault(k, []).append((d, v))
    return {k: sorted(v) for k, v in out.items()}


def parse_boe_votes(content: bytes) -> list[dict[str, Any]]:
    """BoE 'mpcvoting.xlsx', sheet 'Bank Rate Decisions': header row with member names (col C onwards), then one row per
    meeting: B = date, C = Bank Rate decided (fraction), member columns = rate each member voted for (blank = not a
    member). Returns [{date, rate, for, higher, lower, voters: {name: rate%}}]."""
    import openpyxl  # noqa: PLC0415

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb["Bank Rate Decisions"]
    rows = list(ws.iter_rows(values_only=True))
    names: list[str | None] = []
    for r in rows[:10]:
        if r and any(isinstance(c, str) and "Current members" in c for c in r if c):
            names = [(str(c).replace("\n", " ").strip() if isinstance(c, str) else None) for c in r]
            break
    out = []
    for r in rows:
        if len(r) < 3 or not isinstance(r[1], datetime) or not isinstance(r[2], (int, float)):
            continue
        rate = float(r[2])
        voters = {}
        for i in range(3, len(r)):
            v = r[i]
            nm = names[i] if i < len(names) else None
            if isinstance(v, (int, float)) and nm and nm not in ("Past members", "Current members"):
                voters[nm] = round(float(v) * 100, 4)
        rp = round(rate * 100, 4)
        out.append({"date": r[1].date(), "rate": rp, "for": sum(1 for v in voters.values() if abs(v - rp) < 1e-6),
                    "higher": sum(1 for v in voters.values() if v > rp + 1e-6),
                    "lower": sum(1 for v in voters.values() if v < rp - 1e-6), "voters": voters})
    return out


def vote_text(v: dict[str, Any]) -> str:
    n = v["for"] + v["higher"] + v["lower"]
    if not n:
        return ""
    parts = [f"{v['for']}-{v['higher'] + v['lower']}"]
    if v["higher"]:
        parts.append(f"{v['higher']} for higher")
    if v["lower"]:
        parts.append(f"{v['lower']} for lower")
    return parts[0] + (" (" + ", ".join(parts[1:]) + ")" if len(parts) > 1 else " (unanimous)" if n == v["for"] else "")


# =============================================================================================== fetchers

def fetch_bis(codes: Iterable[str], s: Settings) -> dict[str, Pairs]:
    url = BIS_URL.format(codes="+".join(sorted(set(codes))))
    raw = http_get("bis", url, {"startPeriod": START.isoformat(), "format": "csv"}, ttl=12 * 3600, settings=s, timeout=180)
    return parse_bis_csv(raw.decode("utf-8", errors="replace"))


def fetch_ecb(spec: str, s: Settings, start: date = date(1999, 1, 1)) -> Pairs:
    flow, key = spec.split("/", 1)
    raw = http_get("ecb", ECB_URL.format(flow=flow, key=key),
                   {"format": "csvdata", "detail": "dataonly", "startPeriod": start.isoformat()}, settings=s)
    return parse_sdmx_csv_simple(raw.decode("utf-8", errors="replace"))


def fetch_riksbank(series: str, s: Settings, start: date = date(1994, 6, 1)) -> Pairs:
    raw = http_get("riksbank", RIKSBANK_URL.format(series=series, start=start.isoformat()), settings=s)
    return parse_riksbank(json.loads(raw))


def fetch_norges(spec: str, s: Settings) -> Pairs:
    flow, key = spec.split("/", 1)
    raw = http_get("norges", NORGES_URL.format(flow=flow, key=key),
                   {"format": "csv", "startPeriod": START.isoformat(), "locale": "en"}, settings=s)
    return parse_sdmx_csv_simple(raw.decode("utf-8", errors="replace"), sep=";")


def fetch_valet(codes: Iterable[str], s: Settings) -> dict[str, Pairs]:
    raw = http_get("boc", VALET_URL.format(codes=",".join(sorted(set(codes)))), {"start_date": "1990-01-01"}, settings=s)
    return parse_valet(json.loads(raw))


def fetch_fred(ids: Iterable[str], s: Settings, start: date | None = None) -> dict[str, Pairs]:
    from app.ingestion.fred import FredClient  # noqa: PLC0415

    fc = FredClient(s)
    out: dict[str, Pairs] = {}
    for sid in sorted(set(ids)):
        try:
            obs = fc.observations(sid, start)
        except Exception as e:  # noqa: BLE001
            log.warning("cb: FRED %s failed: %s", sid, e)
            continue
        pairs = [(date.fromisoformat(o["date"]), to_float(o["value"])) for o in obs]
        out[sid] = [(d, v) for d, v in pairs if v is not None]
    return out


def fetch_lpr(s: Settings) -> dict[str, Pairs]:
    """Last 12 months of LPR prints (the source refuses longer windows); merged with prints stored by earlier runs
    (state file) so the history accumulates."""
    from pathlib import Path  # noqa: PLC0415

    end = now_utc().date()
    start = end - timedelta(days=364)
    try:
        raw = http_post("lpr", f"{LPR_URL}?lang=CN&strStartDate={start.isoformat()}&strEndDate={end.isoformat()}",
                        settings=s)
        got = parse_lpr(json.loads(raw))
    except Exception as e:  # noqa: BLE001
        log.warning("cb: LPR fetch failed: %s", e)
        got = {}
    p = Path(s.state_dir) / "cb_lpr_prints.json"
    try:
        old = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        old = {}
    merged: dict[str, dict[str, float]] = {k: dict(v) for k, v in old.items()}
    for k, pairs in got.items():
        merged.setdefault(k, {}).update({d.isoformat(): v for d, v in pairs})
    if got:
        p.write_text(json.dumps(merged, indent=0), encoding="utf-8")
    return {k: sorted((date.fromisoformat(d), v) for d, v in m.items()) for k, m in merged.items()}


# =============================================================================================== series assembly

def merge_priority(series: list[tuple[str, Pairs]]) -> list[tuple[date, float, str]]:
    """Combine step series by priority: the first series covers its own date range; earlier history is filled from
    the next series that has it, and newer observations (after the primary's last date) from lower-priority series."""
    series = [(n, sorted(p)) for n, p in series if p]
    if not series:
        return []
    out: dict[date, tuple[float, str]] = {}
    lo, hi = None, None
    for name, pairs in series:
        for d, v in pairs:
            if (lo is None or d < lo) or (hi is None or d > hi):
                out.setdefault(d, (v, name))
        f, l_ = pairs[0][0], pairs[-1][0]
        lo = f if lo is None else min(lo, f)
        hi = l_ if hi is None else max(hi, l_)
    return sorted((d, v, n) for d, (v, n) in out.items())


def fed_mid(fred: dict[str, Pairs]) -> Pairs:
    """Fed target: midpoint of DFEDTARL/DFEDTARU from 2008-12-16; single target DFEDTAR before."""
    lo, up = dict(fred.get("DFEDTARL", [])), dict(fred.get("DFEDTARU", []))
    mid = [(d, round((lo[d] + up[d]) / 2, 4)) for d in sorted(set(lo) & set(up))]
    first = mid[0][0] if mid else date.max
    return [(d, v) for d, v in fred.get("DFEDTAR", []) if d < first] + mid


def change_points(pairs: Iterable[tuple[date, float]], eps: float = CHANGE_EPS) -> list[dict[str, Any]]:
    """[(effective_date, before, after)] for each change of a step series."""
    out, prev = [], None
    for d, v in sorted(pairs):
        if prev is not None and abs(v - prev) >= eps:
            out.append({"effective_date": d, "rate_before": prev, "rate_after": v})
        prev = v
    return out


def derive_decisions(pairs: Pairs, meetings: list[date], today: date, attrib_days: int = ATTRIB_DAYS
                     ) -> list[dict[str, Any]]:
    """Decision history from a policy-rate step series and official meeting dates (see module docstring)."""
    meetings = sorted(set(m for m in meetings if m <= today))
    cal_start = meetings[0] if meetings else None
    used: set[date] = set()
    out: list[dict[str, Any]] = []
    for c in change_points(pairs):
        e = c["effective_date"]
        cands = [m for m in meetings if m <= e <= m + timedelta(days=attrib_days) and m not in used]
        chg = round((c["rate_after"] - c["rate_before"]) * 100, 2)
        row = {"effective_date": e, "rate_before": c["rate_before"], "rate_after": c["rate_after"], "change_bp": chg,
               "action": "hike" if chg > 0 else "cut"}
        if cands:
            m = max(cands)
            used.add(m)
            row.update(decision_date=m, scheduled=True, date_basis="official_meeting")
        elif cal_start is not None and e >= cal_start:
            row.update(decision_date=e, scheduled=False, date_basis="effective_date")
        else:
            row.update(decision_date=e, scheduled=None, date_basis="effective_date")
        out.append(row)
    lookup = sorted(pairs)
    for m in meetings:
        if m in used:
            continue
        before = _asof(lookup, m - timedelta(days=1))
        if before is None:
            continue
        out.append({"effective_date": None, "rate_before": before, "rate_after": before, "change_bp": 0.0,
                    "action": "hold", "decision_date": m, "scheduled": True, "date_basis": "official_meeting"})
    out.sort(key=lambda r: r["decision_date"])
    return out


def _asof(pairs: Pairs, d: date) -> float | None:
    v = None
    for dd, x in pairs:
        if dd > d:
            break
        v = x
    return v


def cycle_phases(changes: list[dict[str, Any]], days: list[date], window: int = CYCLE_WINDOW_DAYS
                 ) -> list[tuple[date, str, date | None, float | None]]:
    """Phase per day: sign of the most recent change within ``window`` days, else 'pausing'.
    ``changes``: [{effective_date|decision_date, change_bp}] (non-zero only)."""
    ev = sorted((c.get("decision_date") or c["effective_date"], c["change_bp"]) for c in changes if c["change_bp"])
    out, j, last = [], 0, None
    for d in sorted(days):
        while j < len(ev) and ev[j][0] <= d:
            last = ev[j]
            j += 1
        if last is not None and (d - last[0]).days <= window:
            ph = "hiking" if last[1] > 0 else "cutting"
        else:
            ph = "pausing"
        out.append((d, ph, last[0] if last else None, last[1] if last else None))
    return out


# =============================================================================================== meetings

def fetch_meetings(s: Settings, today: date) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Official meeting calendars (calendar_sources) + previously stored meetings + macro_releases rows."""
    from app.macro import calendar_sources as cs  # noqa: PLC0415

    srcs = sorted({b.meeting_source for b in BANKS if b.meeting_source})
    entries, rep = cs.fetch_all(date(1999, 1, 1), today + timedelta(days=550), s, sources=srcs)
    by_src = {b.meeting_source: b.code for b in BANKS if b.meeting_source}
    out: dict[str, dict[date, dict[str, Any]]] = {}
    for e in entries:
        if e.kind != "policy_meeting" or e.source_id not in by_src:
            continue
        d = e.scheduled_local.date()
        out.setdefault(by_src[e.source_id], {})[d] = {
            "meeting_date": d, "scheduled_at": e.scheduled_local.astimezone(UTC), "time_known": e.time_known,
            "source_id": e.source_id, "name": e.name, "url": e.url or "", "sep": "SEP" in (e.name or "")}
    for tbl, cols in (("macro_releases", ("source_id", "release_date", "scheduled_at", "release_name", "url", "time_known")),
                      ("cb_meetings", None)):
        df = read_table_safe(tbl, s)
        if df is None or df.is_empty():
            continue
        if tbl == "macro_releases":
            df = df.filter(pl.col("kind") == "policy_meeting").select(*cols)
            for sid, d, at, nm, url, tk in df.iter_rows():
                if sid in by_src:
                    out.setdefault(by_src[sid], {}).setdefault(d, {
                        "meeting_date": d, "scheduled_at": at, "time_known": bool(tk), "source_id": sid, "name": nm,
                        "url": url or "", "sep": "SEP" in (nm or "")})
        else:
            for r in df.iter_rows(named=True):
                out.setdefault(r["bank"], {}).setdefault(r["meeting_date"], {
                    "meeting_date": r["meeting_date"], "scheduled_at": r["scheduled_at"], "time_known": r["time_known"],
                    "source_id": r["source_id"], "name": r["name"], "url": r["url"] or "", "sep": bool(r.get("sep"))})
    return {k: sorted(v.values(), key=lambda x: x["meeting_date"]) for k, v in out.items()}, rep


# =============================================================================================== build

@dataclass
class BankData:
    bank: Bank
    policy: list[tuple[date, float, str]]
    corridor: dict[str, Pairs]
    bis: Pairs
    crosscheck: dict[str, Any]


def collect(s: Settings, report: dict[str, Any]) -> dict[str, BankData]:
    need: dict[str, set[str]] = {}
    for b in BANKS:
        for k, v in list(b.policy) + list(b.corridor.values()):
            if k == "fed_mid":
                need.setdefault("fred", set()).update(v.split("|"))
            else:
                need.setdefault(k, set()).add(v)
    got: dict[tuple[str, str], Pairs] = {}
    srcrep: dict[str, Any] = {}

    def attempt(name: str, fn) -> None:  # noqa: ANN001
        try:
            fn()
            srcrep[name] = "ok"
        except Exception as e:  # noqa: BLE001
            log.warning("cb_rates: source %s failed: %s", name, e)
            srcrep[name] = f"failed: {type(e).__name__}: {str(e)[:200]}"

    bis_codes = {b.bis for b in BANKS}

    def _bis() -> None:
        for k, v in fetch_bis(bis_codes, s).items():
            got[("bis", k)] = v
    attempt("bis", _bis)

    def _fred() -> None:
        for k, v in fetch_fred(need.get("fred", set()), s).items():
            got[("fred", k)] = v
    attempt("fred", _fred)
    for key in sorted(need.get("ecb", set())):
        attempt(f"ecb:{key}", lambda key=key: got.__setitem__(("ecb", key), fetch_ecb(key, s)))
    for key in sorted(need.get("riksbank", set())):
        attempt(f"riksbank:{key}", lambda key=key: got.__setitem__(("riksbank", key), fetch_riksbank(key, s)))
    for key in sorted(need.get("norges", set())):
        attempt(f"norges:{key}", lambda key=key: got.__setitem__(("norges", key), fetch_norges(key, s)))

    def _valet() -> None:
        for k, v in fetch_valet(need.get("valet", set()), s).items():
            got[("valet", k)] = v
    if need.get("valet"):
        attempt("valet", _valet)

    def _lpr() -> None:
        for k, v in fetch_lpr(s).items():
            got[("lpr", k)] = v
    attempt("lpr", _lpr)
    lake = lake_series(need.get("lake", set()), s)
    for k, v in lake.items():
        got[("lake", k)] = v
    srcrep["lake"] = f"{len(lake)}/{len(need.get('lake', set()))} series"
    report["sources"] = srcrep

    out: dict[str, BankData] = {}
    for b in BANKS:
        ser: list[tuple[str, Pairs]] = []
        for k, v in b.policy:
            if k == "fed_mid":
                fr = {x: got.get(("fred", x), []) for x in v.split("|")}
                ser.append(("fred:DFEDTARL/U mid (DFEDTAR before 2008-12-16)", fed_mid(fr)))
            else:
                p = got.get((k, v), [])
                if k == "lake" and p and _is_monthly(p):
                    continue  # monthly own-series: cross-check only (daily change dates come from BIS)
                ser.append((f"{k}:{v}", p))
        bis = got.get(("bis", b.bis), [])
        ser.append((f"bis:WS_CBPOL.D.{b.bis}", bis))
        pol = merge_priority(ser)
        cor = {role: got.get(spec, []) for role, spec in b.corridor.items()}
        cc: dict[str, Any] = {}
        if pol and bis:
            d_last = min(pol[-1][0], bis[-1][0])
            pv, bv = _asof([(d, v) for d, v, _ in pol], d_last), _asof(bis, d_last)
            if pv is not None and bv is not None:
                cc = {"date": d_last.isoformat(), "official": pv, "bis": bv, "diff_bp": round((pv - bv) * 100, 2)}
        for k, v in b.policy:  # monthly own series as an extra cross-check
            p = got.get((k, v), [])
            if k == "lake" and p and _is_monthly(p) and pol:
                d_last = p[-1][0]
                pv = _asof([(d, x) for d, x, _ in pol], d_last + timedelta(days=27))
                cc["own_monthly"] = {"series": v, "date": d_last.isoformat(), "value": p[-1][1],
                                     "daily_at_month_end": pv}
        out[b.code] = BankData(b, pol, cor, bis, cc)
    return out


def _is_monthly(p: Pairs) -> bool:
    if len(p) < 3:
        return False
    gaps = [(p[i + 1][0] - p[i][0]).days for i in range(max(0, len(p) - 13), len(p) - 1)]
    return sorted(gaps)[len(gaps) // 2] >= 25


def build_rates(s: Settings | None = None, today: date | None = None, write_out: bool = True) -> dict[str, Any]:
    s = s or get_settings()
    today = today or now_utc().date()
    now = now_utc()
    report: dict[str, Any] = {"today": today.isoformat()}
    data = collect(s, report)
    meetings, mrep = fetch_meetings(s, today)
    report["meeting_sources"] = {k: (v.get("status"), v.get("n")) for k, v in mrep.items()}
    votes: dict[date, dict[str, Any]] = {}
    try:
        votes = {v["date"]: v for v in parse_boe_votes(http_get("boe_votes", BOE_VOTES_URL, ttl=24 * 3600,
                                                                 settings=s, browser=True))}
        report["boe_votes"] = len(votes)
    except Exception as e:  # noqa: BLE001
        report["boe_votes"] = f"failed: {e}"
    # BoE voting history doubles as the official MPC meeting list since 1997
    if votes:
        bm = {m["meeting_date"]: m for m in meetings.get("boe", [])}
        for d in votes:
            bm.setdefault(d, {"meeting_date": d, "scheduled_at": datetime(d.year, d.month, d.day, 11, 0, tzinfo=UTC),
                              "time_known": False, "source_id": "boe_mpc_votes", "name": "MPC Bank Rate decision",
                              "url": BOE_VOTES_URL, "sep": False})
        meetings["boe"] = sorted(bm.values(), key=lambda x: x["meeting_date"])

    days = [d for d in (START + timedelta(days=i) for i in range((today - START).days + 1)) if d.weekday() < 5]
    rate_rows, dec_rows, meet_rows, cyc_rows, summary = [], [], [], [], {}
    day_df = pl.DataFrame({"date": days}, schema={"date": pl.Date})
    for code, bd in data.items():
        pol = bd.policy
        if not pol:
            summary[code] = {"status": "no data"}
            continue
        pairs = [(d, v) for d, v, _ in pol]
        since = bd.bank.decisions_since
        dpairs = [p for p in pairs if p[0] >= since]
        prior = [p for p in pairs if p[0] < since][-1:]
        mlist = meetings.get(code, [])
        decs = derive_decisions(prior + dpairs, [m["meeting_date"] for m in mlist if m["meeting_date"] >= since], today)
        decs = [r for r in decs if r["decision_date"] >= since]
        for r in decs:
            v = votes.get(r["decision_date"]) if code == "boe" else None
            dec_rows.append({"bank": code, "decision_date": r["decision_date"], "effective_date": r["effective_date"],
                             "rate_before": r["rate_before"], "rate_after": r["rate_after"], "change_bp": r["change_bp"],
                             "action": r["action"], "scheduled": r["scheduled"], "date_basis": r["date_basis"],
                             "vote": vote_text(v) if v else None,
                             "vote_detail": json.dumps(v["voters"]) if v else None,
                             "vote_source": "boe:mpcvoting.xlsx" if v else None,
                             "method_version": METHOD_DECISIONS, "computed_at": now})
        for m in mlist:
            meet_rows.append({"bank": code, "meeting_date": m["meeting_date"], "scheduled_at": m["scheduled_at"],
                              "time_known": bool(m["time_known"]), "source_id": m["source_id"], "name": m["name"],
                              "url": m["url"], "sep": bool(m.get("sep")), "is_past": m["meeting_date"] <= today,
                              "computed_at": now})
        # daily rows (business days from the first observation)
        first = pairs[0][0]
        dd = day_df.filter(pl.col("date") >= first)
        frame = dd.join_asof(_pairs_df(pairs, "policy_rate", pol), on="date", strategy="backward")
        for role, cp in bd.corridor.items():
            if cp:
                tol = "14d" if role == "overnight" else None
                frame = frame.join_asof(_pairs_df(cp, role), on="date", strategy="backward", tolerance=tol)
        if bd.bis:
            frame = frame.join_asof(_pairs_df(bd.bis, "bis_rate"), on="date", strategy="backward")
        for col in ("lower", "upper", "deposit", "lending", "mro", "overnight", "bis_rate"):
            if col not in frame.columns:
                frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))
        rate_rows.append(frame.with_columns(pl.lit(code).alias("bank"), pl.lit(now).alias("computed_at"))
                         .select("bank", "date", "policy_rate", "lower", "upper", "deposit", "lending", "mro",
                                 "overnight", "bis_rate", "source", "obs_date", "computed_at"))
        moves = [r for r in decs if r["change_bp"]]
        for d, ph, lcd, lbp in cycle_phases(moves, dd.filter(pl.col("date") >= since).get_column("date").to_list()):
            cyc_rows.append((code, d, ph, lcd, lbp))
        last_move = moves[-1] if moves else None
        upcoming = [m["meeting_date"] for m in mlist if m["meeting_date"] > today]
        summary[code] = {"rate": pairs[-1][1], "as_of": pairs[-1][0].isoformat(), "source": pol[-1][2],
                         "last_move": (last_move["decision_date"].isoformat(), last_move["change_bp"]) if last_move else None,
                         "next_meeting": upcoming[0].isoformat() if upcoming else None, "decisions": len(decs),
                         "phase": cyc_rows[-1][2] if cyc_rows else None, "crosscheck": bd.crosscheck}
    rates = pl.concat(rate_rows, how="vertical_relaxed") if rate_rows else pl.DataFrame()
    dec = pl.DataFrame(dec_rows, schema=DEC_SCHEMA)
    meet = pl.DataFrame(meet_rows, schema=MEET_SCHEMA).unique(["bank", "meeting_date"], keep="first")
    cyc = pl.DataFrame(cyc_rows, schema={"bank": pl.Utf8, "date": pl.Date, "phase": pl.Utf8,
                                         "last_change_date": pl.Date, "last_change_bp": pl.Float64}, orient="row")
    if not rates.is_empty():
        cyc = cyc.join(rates.select("bank", "date", "policy_rate"), on=["bank", "date"], how="left")
    cyc = cyc.with_columns(pl.lit(METHOD_CYCLE).alias("method_version"), pl.lit(now).alias("computed_at"))
    report.update(banks=len(summary), rate_rows=rates.height, decisions=dec.height, meetings=meet.height,
                  cycle_rows=cyc.height, summary=summary)
    if write_out:
        report["written"] = {"cb_policy_rates": write(rates, "cb_policy_rates", s),
                             "cb_decisions": write(dec, "cb_decisions", s),
                             "cb_meetings": write(meet, "cb_meetings", s),
                             "cb_rate_cycle": write(cyc, "cb_rate_cycle", s)}
    return report


def _pairs_df(pairs: Pairs, name: str, pol: list[tuple[date, float, str]] | None = None) -> pl.DataFrame:
    if pol is not None:
        return pl.DataFrame({"date": [d for d, _, _ in pol], name: [v for _, v, _ in pol], "source": [n for _, _, n in pol],
                             "obs_date": [d for d, _, _ in pol]},
                            schema={"date": pl.Date, name: pl.Float64, "source": pl.Utf8, "obs_date": pl.Date}).sort("date")
    return pl.DataFrame({"date": [d for d, _ in pairs], name: [v for _, v in pairs]},
                        schema={"date": pl.Date, name: pl.Float64}).unique("date", keep="last").sort("date")


DEC_SCHEMA = {"bank": pl.Utf8, "decision_date": pl.Date, "effective_date": pl.Date, "rate_before": pl.Float64,
              "rate_after": pl.Float64, "change_bp": pl.Float64, "action": pl.Utf8, "scheduled": pl.Boolean,
              "date_basis": pl.Utf8, "vote": pl.Utf8, "vote_detail": pl.Utf8, "vote_source": pl.Utf8,
              "method_version": pl.Utf8, "computed_at": pl.Datetime("us", "UTC")}
MEET_SCHEMA = {"bank": pl.Utf8, "meeting_date": pl.Date, "scheduled_at": pl.Datetime("us", "UTC"), "time_known": pl.Boolean,
               "source_id": pl.Utf8, "name": pl.Utf8, "url": pl.Utf8, "sep": pl.Boolean, "is_past": pl.Boolean,
               "computed_at": pl.Datetime("us", "UTC")}


def run(ctx):  # noqa: ANN001 - JobContext
    """Job cb_rates (nightly): policy rates, corridors, decisions, meetings, rate-cycle phase."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = build_rates(ctx.settings)
    failed = [k for k, v in rep.get("sources", {}).items() if str(v).startswith("failed")]
    update_state({"items": {"W3.1": "done"}, "last_runs": {"cb_rates": {
        "at": now_utc().isoformat(timespec="seconds"), "banks": rep["banks"], "decisions": rep["decisions"],
        "failed_sources": failed}}}, ctx.settings)
    return JobResult(status="partial" if failed else "success", rows_written=rep.get("written", {}),
                     entities_ok=rep["banks"], entities_failed=len(failed),
                     message=json.dumps({k: rep[k] for k in ("banks", "rate_rows", "decisions", "meetings")} |
                                        {"failed": failed}, default=str)[:2000])
