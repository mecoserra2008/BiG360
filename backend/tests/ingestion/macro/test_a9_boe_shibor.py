"""COVERAGE_V3 A9: the boe macro fetcher no longer touches the robots-disallowed IADB (gilts from the yield-curve
archive, Bank Rate from the MPC voting record + BIS, SONIA via FRED; IADB-only codes frozen) and SHIBOR comes from
CFETS / chinamoney instead of AKShare's Jin10 upstream. Recorded fixtures + synthetic workbooks, no network."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date, datetime
from pathlib import Path

import pytest

from app.core.robots_policy import find_block
from app.ingestion import macro_national as MN
from app.ingestion.macro_common import GSeries, MacroRunner

FIX = Path(__file__).parent / "fixtures"
RATES_FIX = Path(__file__).resolve().parents[2] / "rates" / "fixtures"


class Resp:
    def __init__(self, content: bytes = b"", payload: object = None) -> None:
        self.content, self._p, self.status_code = content, payload, 200
        self.text = content.decode("utf-8", errors="replace")

    def json(self) -> object:
        return self._p


class Http:
    def __init__(self, routes: dict[str, object]) -> None:
        self.routes, self.calls = routes, []

    def _find(self, url: str) -> object:
        assert find_block(url) is None, f"robots-blocked request: {url}"
        for k, v in self.routes.items():
            if k in url:
                return v
        raise AssertionError(f"unexpected request {url}")

    def get(self, url: str, params: dict | None = None, **kw: object) -> Resp:
        self.calls.append((url, params))
        v = self._find(url)
        return v(params or {}) if callable(v) else v

    def download(self, url: str, dest: Path, *a: object, **k: object) -> Path:
        self.calls.append((url, None))
        dest.write_bytes(self._find(url).content)
        return dest


def _spec(sid: str, freq: str = "D", src: str = "boe", **req: object) -> GSeries:
    return GSeries(sid, src, sid, sid, "GB", "rates", freq, "percent", "level", 1, req=dict(req))


def _yc_zip(member: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(member, (RATES_FIX / "boe_nominal_current.xlsx").read_bytes())
    return buf.getvalue()


def _votes_xlsx() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Bank Rate Decisions"
    ws.append([None, None, "Current members", "A Member", "B Member"])
    ws.append([None, datetime(2026, 9, 17), 0.04, 0.04, 0.04])
    ws.append([None, datetime(2026, 8, 7), 0.04, 0.04, 0.0425])
    ws.append([None, datetime(2026, 5, 8), 0.0425, 0.0425, 0.0425])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def _runner(tmp_data, http: Http, start: date) -> MacroRunner:
    r = MacroRunner("test", tmp_data, start)
    r.http = lambda src, **kw: http  # type: ignore[method-assign]
    return r


def test_boe_routes_use_allowed_files_only(tmp_data, monkeypatch):
    monkeypatch.setattr(type(tmp_data), "secret", lambda self, k: "k" if k == "fred_apikey" else None)
    bis_csv = "KEY,FREQ,REF_AREA,TIME_PERIOD,OBS_VALUE\nWS_CBPOL.D.GB,D,GB,2026-04-30,4.5\n"
    http = Http({
        "latest-yield-curve-data.zip": Resp(_yc_zip("GLC Nominal daily data current month.xlsx")),
        "glcnominalddata.zip": Resp(_yc_zip("GLC Nominal daily data_2025 to present.xlsx")),
        "mpcvoting.xlsx": Resp(_votes_xlsx()),
        "stats.bis.org": Resp(bis_csv.encode()),
        "api.stlouisfed.org": Resp(payload={"observations": [{"date": "2026-10-01", "value": "3.9714"},
                                                             {"date": "2026-10-02", "value": "."}]}),
    })
    r = _runner(tmp_data, http, date(2026, 4, 1))
    specs = [_spec("boe:IUDMNZC", code="IUDMNZC"), _spec("boe:IUDSNZC", code="IUDSNZC"),
             _spec("boe:IUDBEDR", code="IUDBEDR"), _spec("boe:IUDSOIA", code="IUDSOIA"),
             _spec("boe:IUDMNPY", code="IUDMNPY"), _spec("boe:LPMVQJW", "M", code="LPMVQJW")]
    MN.fetch_boe(r, specs)
    urls = [u for u, _ in http.calls]
    assert not any("boeapps" in u.lower() for u in urls)
    assert set(r.report["skipped_series"]) == {"boe:IUDMNPY", "boe:LPMVQJW"}
    assert all(v.startswith("source_disallowed") for v in r.report["skipped_series"].values())
    assert set(r.report["ok"]) == {"boe:IUDMNZC", "boe:IUDSNZC", "boe:IUDBEDR", "boe:IUDSOIA"}, r.report
    _, obs = r.finalize()
    got = {(x["series_id"], x["date"]): x["value"] for x in obs.iter_rows(named=True)}
    # the 10Y (120M) nominal spot of the workbook = the IADB IUDMNZC series, 4 dp as the IADB
    from app.rates.sources import parse_boe_workbook

    spot = parse_boe_workbook((RATES_FIX / "boe_nominal_current.xlsx").read_bytes(), (120,))["spot"]
    assert got[("boe:IUDMNZC", date(2026, 10, 1))] == round(spot[date(2026, 10, 1)][120], 4)
    # Bank Rate: BIS before the first MPC decision in the record, then step series on London sessions
    assert got[("boe:IUDBEDR", date(2026, 5, 7))] == 4.5
    assert got[("boe:IUDBEDR", date(2026, 5, 8))] == 4.25 and got[("boe:IUDBEDR", date(2026, 8, 7))] == 4.0
    assert ("boe:IUDBEDR", date(2026, 5, 25)) not in got          # Spring bank holiday (XLON closed)
    assert ("boe:IUDBEDR", date(2026, 5, 9)) not in got           # Saturday
    assert got[("boe:IUDSOIA", date(2026, 10, 1))] == 3.9714 and ("boe:IUDSOIA", date(2026, 10, 2)) not in got


def test_boe_sonia_without_fred_key_fails_cleanly(tmp_data, monkeypatch):
    monkeypatch.setattr(type(tmp_data), "secret", lambda self, k: None)
    http = Http({})
    r = _runner(tmp_data, http, date(2026, 1, 1))
    MN.fetch_boe(r, [_spec("boe:IUDSOIA", code="IUDSOIA")])
    assert http.calls == [] and "FRED_APIKEY" in r.report["failed"]["boe:IUDSOIA"]


def test_bank_rate_daily_full_history_uses_xlon_sessions():
    """Regression: a start before XLON's first session (1990-01-01) must not drop to the weekday fallback."""
    dec = [(date(2017, 1, 1), 0.25)]
    out = dict(MN.bank_rate_daily(dec, [(date(1989, 10, 5), 15.0)], date(1990, 1, 1), date(2017, 4, 30)))
    assert date(1990, 1, 1) not in out and out[date(1990, 1, 2)] == 15.0
    assert date(2017, 4, 14) not in out and date(2017, 4, 17) not in out   # Good Friday, Easter Monday
    assert out[date(2017, 4, 13)] == 0.25 and out[date(2017, 4, 18)] == 0.25


def _gap_runner(tmp_data, monkeypatch, archive: bytes | None, archive_mtime: date | None, new_archive: bytes,
                stored_until: date, today: date = date(2026, 11, 6)):
    """Month boundary: the latest zip holds November only (2-5 Nov); the store ends at `stored_until`."""
    import os

    def fake_zip(content, months, min_year=None):
        raw = content if isinstance(content, bytes) else content.read()
        if raw == b"latest":
            return {date(2026, 11, d): {120: 5.0 + d / 100} for d in (2, 3, 4, 5)}
        days = {b"sep": (29, 30), b"oct": (29, 30)}[raw]
        mon = 9 if raw == b"sep" else 10
        return {date(2026, mon, d): {120: 5.2 + d / 1000} for d in days}

    monkeypatch.setattr(MN, "_boe_curve_from_zip", fake_zip)
    dest = tmp_data.raw_dir / "rates" / "boe" / "glcnominalddata.zip"
    if archive is not None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(archive)
        ts = datetime(archive_mtime.year, archive_mtime.month, archive_mtime.day, 12).timestamp()
        os.utime(dest, (ts, ts))
    http = Http({"latest-yield-curve-data.zip": Resp(b"latest"), "glcnominalddata.zip": Resp(new_archive)})
    r = MacroRunner("test", tmp_data)
    r.http = lambda src, **kw: http  # type: ignore[method-assign]
    r.today = today
    r._last_obs = {"boe:IUDMNZC": stored_until}
    stored = {d for d in MN.xlon_sessions(date(2026, 8, 1), stored_until)}
    monkeypatch.setattr(MN, "_boe_stored_dates", lambda r_, ids, lo: {i: {d for d in stored if d >= lo} for i in ids})
    return r, http


def _days(r) -> list[date]:
    _, obs = r.finalize()
    return sorted(obs["date"].to_list())


def test_boe_yc_month_end_day_missed_by_nightly_is_filled_from_archive(tmp_data, monkeypatch):
    """Regression (A9 review): stored last = Oct 29, latest = Nov 2-5; Oct 30 (on disk in the archive) was dropped
    because a 4-calendar-day tolerance on max(date) hid the 1-session gap."""
    r, http = _gap_runner(tmp_data, monkeypatch, b"oct", date(2026, 11, 3), b"unused", date(2026, 10, 29))
    MN._boe_yield_curve(r, [_spec("boe:IUDMNZC", code="IUDMNZC")])
    assert date(2026, 10, 30) in _days(r) and [u for u, _ in http.calls] == [MN.BOE_YC_LATEST]   # no re-download
    assert r.report["boe_yc_gaps"]["boe:IUDMNZC"].endswith("2026-10-30")


def test_boe_yc_stale_archive_is_refetched_when_it_ends_before_the_gap(tmp_data, monkeypatch):
    """The copy on disk (3 days old, so the weekly rule would keep it) ends in September: fetched again once."""
    r, http = _gap_runner(tmp_data, monkeypatch, b"sep", date(2026, 11, 3), b"oct", date(2026, 10, 29))
    MN._boe_yield_curve(r, [_spec("boe:IUDMNZC", code="IUDMNZC")])
    assert [u for u, _ in http.calls] == [MN.BOE_YC_LATEST, MN.BOE_YC_ARCHIVE]
    assert date(2026, 10, 30) in _days(r)


def test_boe_yc_archive_refetch_at_most_daily_and_hole_retried_next_night(tmp_data, monkeypatch):
    """Downloaded today and still behind (the BoE has not updated it yet): no second download today; the hole stays
    detectable from the stored dates (not the watermark), so the next night tries again."""
    r, http = _gap_runner(tmp_data, monkeypatch, b"sep", date(2026, 11, 6), b"oct", date(2026, 10, 29))
    MN._boe_yield_curve(r, [_spec("boe:IUDMNZC", code="IUDMNZC")])
    assert [u for u, _ in http.calls] == [MN.BOE_YC_LATEST] and date(2026, 10, 30) not in _days(r)
    # next night: November rows are stored now (watermark Nov 5) but Oct 30 is still missing -> found and fetched
    r2, http2 = _gap_runner(tmp_data, monkeypatch, b"sep", date(2026, 11, 6), b"oct", date(2026, 10, 29),
                            today=date(2026, 11, 7))
    r2._last_obs = {"boe:IUDMNZC": date(2026, 11, 5)}
    MN._boe_yield_curve(r2, [_spec("boe:IUDMNZC", code="IUDMNZC")])
    assert [u for u, _ in http2.calls] == [MN.BOE_YC_LATEST, MN.BOE_YC_ARCHIVE] and date(2026, 10, 30) in _days(r2)


def test_boe_yc_complete_store_reads_the_current_workbook_only(tmp_data, monkeypatch):
    r, http = _gap_runner(tmp_data, monkeypatch, None, None, b"unused", date(2026, 10, 30))
    MN._boe_yield_curve(r, [_spec("boe:IUDMNZC", code="IUDMNZC")])
    assert [u for u, _ in http.calls] == [MN.BOE_YC_LATEST] and "boe_yc_gaps" not in r.report
    assert _days(r) == [date(2026, 11, d) for d in (2, 3, 4, 5)]


def test_boe_stored_dates_reads_the_boe_partition(tmp_data):
    from app.core.storage import write_table
    from app.ingestion.macro_common import obs_frame

    sp = _spec("boe:IUDMNZC", code="IUDMNZC")
    write_table(obs_frame(sp, [(date(2026, 10, 28), 5.3), (date(2026, 10, 29), 5.28), (date(2026, 9, 1), 5.1)],
                          date(2026, 11, 6)), "macro_observations", settings=tmp_data)
    r = MacroRunner("test", tmp_data)
    got = MN._boe_stored_dates(r, ["boe:IUDMNZC", "boe:IUDSNZC"], date(2026, 10, 1))
    assert got == {"boe:IUDMNZC": {date(2026, 10, 28), date(2026, 10, 29)}, "boe:IUDSNZC": set()}


def test_parse_shibor_his_fixture():
    j = json.loads((FIX / "chinamoney_shiborhis_sample.json").read_text(encoding="utf-8"))
    d = MN.parse_shibor_his(j)
    assert set(d) >= {"ON", "1W", "3M", "1Y"} and d["3M"][-1] == (date(2026, 11, 6), 1.398)
    assert all(isinstance(x[0], date) for v in d.values() for x in v)
    assert MN.parse_shibor_his(None) == {} and MN.parse_shibor_his({"records": []}) == {}


def test_shibor_from_cfets_not_akshare(tmp_data, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "akshare", None)       # any AKShare import would raise ImportError
    j = json.loads((FIX / "chinamoney_shiborhis_sample.json").read_text(encoding="utf-8"))
    http = Http({"chinamoney.com.cn/ags/ms/cm-u-bk-shibor/ShiborHis": Resp(payload=j)})
    r = _runner(tmp_data, http, date(2026, 10, 1))
    sp = _spec("akshare_cn:shibor_3m", src="akshare_cn", fn="macro_china_shibor_all", col="3M-定价", date_col="日期")
    MN.fetch_akshare_cn(r, [sp])
    assert [p["startDate"] for _, p in http.calls] == ["2026-10-01"] and r.report["ok"]["akshare_cn:shibor_3m"] >= 1
    _, obs = r.finalize()
    assert obs.filter(obs["date"] == date(2026, 11, 6))["value"].to_list() == [pytest.approx(1.398)]
    assert "macro_china_shibor_all" in MN.AK_DISALLOWED
