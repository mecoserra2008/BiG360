"""International-organisation macro -> macro_series + macro_observations. Job `macro_intl`.

Sources (keyless; series + request parameters in contracts/macro_catalog.yaml):
  oecd          OECD SDMX REST (sdmx.oecd.org). HARD LIMIT 60 data downloads / hour / IP (shared "oecd" bucket,
                cross-process): every dataflow is fetched in ONE request with all countries '+'-joined, and responses
                are disk-cached for 20 h, so a nightly run costs ~5 downloads. CLI / BCI / CCI, KEI-type short-term
                indicators for non-EU countries, quarterly GDP growth.
  bis           BIS SDMX v2: policy rates (WS_CBPOL), credit-to-GDP gap (WS_CREDIT_GAP), residential property prices
                (WS_SPP), effective exchange rates (WS_EER), total credit (WS_TC).
  imf           IMF SDMX (api.imf.org; dataservices.imf.org is gone): CPI and other STA dataflows for gaps.
  worldbank     World Bank v2 API (annual structural: GDP growth, debt, current account).
  wb_pinksheet  World Bank commodity price data ("Pink Sheet", monthly xlsx), discovered via the WB data-catalog API.
"""

from __future__ import annotations

import io
import re
from datetime import UTC, date, datetime
from typing import Any

from app.core.http import HttpError
from app.ingestion.macro_common import GSeries, MacroRunner, fetcher, parse_period, run_job, sdmx_fetch, to_float

SOURCES = ["oecd", "bis", "imf", "worldbank", "wb_pinksheet"]


@fetcher("oecd")
def fetch_oecd(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: flow 'AGENCY,DSD@DF,VER', key 'D1.D2...' (one request per flow; countries '+'-joined)."""
    sdmx_fetch(r, specs, "oecd", lambda flow, key: f"https://sdmx.oecd.org/public/rest/data/{flow}/{key}",
               {"startPeriod": r.since(), "format": "csvfile"}, cache_ttl=20 * 3600, max_batch=200)


@fetcher("bis")
def fetch_bis(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: flow (e.g. WS_CBPOL), key (e.g. 'M.US'), ver (default 1.0)."""
    sdmx_fetch(r, specs, "bis",
               lambda flow, key: f"https://stats.bis.org/api/v2/data/dataflow/BIS/{flow}/{_ver(specs, flow)}/{key}",
               {"startPeriod": r.since(), "format": "csv"}, cache_ttl=12 * 3600, max_batch=80, single_dim=True)


def _ver(specs: list[GSeries], flow: str) -> str:
    return next((str(s.req.get("ver", "1.0")) for s in specs if s.req.get("flow") == flow), "1.0")


@fetcher("imf")
def fetch_imf(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: flow 'IMF.STA,CPI' (agency,dataflow[,version]), key."""
    sdmx_fetch(r, specs, "imf", lambda flow, key: f"https://api.imf.org/external/sdmx/2.1/data/{flow}/{key}",
               {"startPeriod": r.since()}, headers={"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"},
               cache_ttl=12 * 3600, max_batch=60, single_dim=True)


@fetcher("worldbank")
def fetch_worldbank(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: indicator, code (ISO3 or WB code). One request per indicator with ';'-joined countries."""
    groups: dict[str, list[GSeries]] = {}
    for sp in specs:
        groups.setdefault(sp.req["indicator"], []).append(sp)
    for ind, grp in groups.items():
        def one(ind: str = ind, grp: list[GSeries] = grp) -> None:
            codes = ";".join(sorted({str(sp.req["code"]) for sp in grp}))
            j = r.http("worldbank").get(f"https://api.worldbank.org/v2/country/{codes}/indicator/{ind}",
                                        {"format": "json", "per_page": 20000,
                                         "date": f"{r.start.year}:{datetime.now(UTC).year}"}, cache_ttl=24 * 3600).json()
            data = parse_worldbank(j)
            for sp in grp:
                pairs = data.get(str(sp.req["code"]).upper())
                if pairs:
                    r.add(sp, pairs)
                else:
                    r.fail(sp, f"{sp.req['code']} not in World Bank {ind}")
        r.attempt(f"worldbank:{ind}", one, grp)


def parse_worldbank(j: Any) -> dict[str, list[tuple[date, float | None]]]:
    out: dict[str, list[tuple[date, float | None]]] = {}
    if not isinstance(j, list) or len(j) < 2 or not j[1]:
        return out
    for row in j[1]:
        try:
            d = parse_period(str(row["date"]))
        except (KeyError, ValueError):
            continue
        for code in {str(row.get("countryiso3code") or "").upper(), str((row.get("country") or {}).get("id", "")).upper()}:
            if code:
                out.setdefault(code, []).append((d, to_float(row.get("value"))))
    return {k: sorted(v) for k, v in out.items()}


# ------------------------------------------------------------------------------------------------ Pink Sheet

PINK_DATASET = "0037798"   # WB data catalog: "Commodity Prices (Pink Sheet)"


@fetcher("wb_pinksheet")
def fetch_pinksheet(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: column (Pink Sheet 'Monthly Prices' commodity name, e.g. 'Crude oil, Brent'), url (pinned xlsx URL; the
    thedocs doc id changes every January -> on 404 the WB data-catalog API is asked for the current resource)."""
    def one() -> None:
        from app.core.http import HttpError as _HE  # noqa: PLC0415

        pinned = next((str(sp.req["url"]) for sp in specs if sp.req.get("url")), None)
        try:
            if not pinned:
                raise _HE(404, "catalog", "no pinned url")
            resp = r.http("worldbank").get(pinned, cache_ttl=24 * 3600)
        except _HE:
            resp = r.http("worldbank").get(pinksheet_url(r), cache_ttl=24 * 3600)
        cols = parse_pinksheet(resp.content)
        for sp in specs:
            pairs = cols.get(norm_pink(sp.req["column"]))
            if pairs:
                r.add(sp, pairs)
            else:
                r.fail(sp, f"column {sp.req['column']} not in Pink Sheet")
    r.attempt("wb_pinksheet", one, specs)


def pinksheet_url(r: MacroRunner) -> str:
    """Current CMO-Historical-Data-Monthly.xlsx URL from the WB data-catalog API (no HTML scraping)."""
    j = r.http("worldbank").get("https://datacatalogapi.worldbank.org/ddhxext/DatasetDownload",
                                {"dataset_unique_id": PINK_DATASET, "version_id": ""}, cache_ttl=24 * 3600,
                                raise_for_status=False)
    try:
        for res in (j.json() or {}).get("resources", []) or []:
            u = str(res.get("distribution", {}).get("url") or res.get("url") or "")
            if "Monthly" in u and u.lower().endswith(".xlsx"):
                return u
    except Exception:  # noqa: BLE001, S110
        pass
    j = r.http("worldbank").get("https://datacatalogapi.worldbank.org/ddhxext/ResourceView",
                                {"dataset_unique_id": PINK_DATASET}, cache_ttl=24 * 3600, raise_for_status=False)
    txt = j.text
    import re  # noqa: PLC0415

    m = re.search(r'https://[^"\s]+CMO-Historical-Data-Monthly\.xlsx', txt)
    if m:
        return m.group(0)
    raise HttpError(404, "datacatalogapi", "Pink Sheet monthly xlsx not found")


def norm_pink(name: str) -> str:
    return re.sub(r"\s*\*+\s*$", "", str(name)).strip()


def parse_pinksheet(content: bytes) -> dict[str, list[tuple[date, float | None]]]:
    """'Monthly Prices' sheet (select by NAME: the first sheet is 'Mismatch Details'): title rows, a commodity-name
    row (first cell blank; names may carry trailing blanks or ' **'), a units row, then rows '2026M09' with values
    ('...' U+2026 = n/a). Keys are normalised names ('Crude oil, Brent')."""
    import openpyxl  # noqa: PLC0415

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb["Monthly Prices"]
    names: list[str] | None = None
    out: dict[str, list[tuple[date, float | None]]] = {}
    for row in ws.iter_rows(values_only=True):
        first = str(row[0]).strip() if row and row[0] is not None else ""
        if names is None:
            vals = [norm_pink(c) if c is not None else "" for c in row[1:]]
            if not first and sum(1 for v in vals if v and not v.startswith("(")) >= 10:
                names = vals
            continue
        if re.fullmatch(r"\d{4}M\d{2}", first):
            d = date(int(first[:4]), int(first[5:]), 1)
            for c, v in zip(names, row[1:], strict=False):
                if c:
                    out.setdefault(c, []).append((d, to_float(v)))
    wb.close()
    return out


def run(ctx):  # noqa: ANN001 - JobContext
    return run_job(ctx, "macro_intl", SOURCES)
