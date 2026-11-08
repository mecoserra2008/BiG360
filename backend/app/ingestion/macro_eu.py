"""Europe macro -> macro_series + macro_observations. Job `macro_eu`.

Sources (all keyless; series + request parameters in contracts/macro_catalog.yaml):
  ecb         ECB Data Portal SDMX (csvdata): FM policy rates / Euribor, YC AAA + all-issuer curves, HICP, BSI money &
              loans, MIR lending rates, CISS stress indices, MNA GDP, IRS convergence 10y yields, RESR house prices.
  eurostat    dissemination API JSON-stat 2.0, one request per (dataset, filters) with several geo values (EA + DE FR IT
              ES PT NL ...): HICP (prc_hicp_minr), PPI, GDP, IP, retail, ESI/confidence, unemployment, employment,
              house prices, permits, government debt/deficit, BoP, 10y convergence yields.
  bundesbank  Bundesbank SDMX REST (daily Bund yields from the term-structure estimates).
  snb         SNB data portal cubes (policy rate, Confederation yields, CPI).
  insee / ine_pt / bpstat / ine_es / istat
              national statistics offices / central banks for gaps (see catalog).
"""

from __future__ import annotations

import csv
import io
from dataclasses import replace
from datetime import date
from typing import Any

from app.core.http import HttpError
from app.core.logsetup import get_logger
from app.ingestion.macro_common import (
    GSeries,
    MacroRunner,
    fetcher,
    parse_jsonstat_multi,
    parse_period,
    run_job,
    sdmx_fetch,
    to_float,
)

log = get_logger(__name__)
SOURCES = ["ecb", "eurostat", "bundesbank", "snb", "insee", "bpstat", "ine_es"]
EUROSTAT = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"


@fetcher("ecb")
def fetch_ecb(r: MacroRunner, specs: list[GSeries]) -> None:
    """key 'FLOW/D1.D2...' -> grouped per flow, '+' unions per dimension."""
    norm = []
    for sp in specs:
        flow, key = str(sp.req["key"]).split("/", 1)
        norm.append(replace(sp, req={**sp.req, "flow": flow, "k": key}))
    sdmx_fetch(r, norm, "ecb", lambda flow, key: f"https://data-api.ecb.europa.eu/service/data/{flow}/{key}",
               {"format": "csvdata", "startPeriod": r.since()}, cache_ttl=6 * 3600, max_batch=40, key_field="k", single_dim=True)


@fetcher("eurostat")
def fetch_eurostat(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: dataset, filters {dim: value}, code (geo) [, alt_codes: [..] tried when code has no data]."""
    groups: dict[tuple[str, tuple], list[GSeries]] = {}
    for sp in specs:
        groups.setdefault((sp.req["dataset"], tuple(sorted((sp.req.get("filters") or {}).items()))), []).append(sp)
    for (ds, filt), grp in groups.items():
        def one(ds: str = ds, filt: tuple = filt, grp: list[GSeries] = grp) -> None:
            geos = sorted({g for sp in grp for g in [str(sp.req["code"]), *map(str, sp.req.get("alt_codes", []))]})
            params: dict[str, Any] = {**dict(filt), "format": "JSON", "lang": "EN", "geo": geos,
                                      "sinceTimePeriod": eurostat_since(r.start, grp[0].frequency)}
            geo_errors: dict[str, str] = {}
            try:
                data = parse_jsonstat_multi(r.http("eurostat").get(f"{EUROSTAT}/{ds}", params, cache_ttl=6 * 3600).json())
            except HttpError:
                data = {}
                for g in geos:  # one bad geo must not sink the batch; its error is kept for the series (F24)
                    try:
                        data.update(parse_jsonstat_multi(r.http("eurostat").get(
                            f"{EUROSTAT}/{ds}", {**params, "geo": [g]}, cache_ttl=6 * 3600).json()))
                    except Exception as e:  # noqa: BLE001
                        geo_errors[g] = f"{type(e).__name__}: {e}"[:160]
                        log.warning("eurostat %s geo %s failed: %s", ds, g, geo_errors[g])
            for sp in grp:
                for g in [str(sp.req["code"]), *map(str, sp.req.get("alt_codes", []))]:
                    pairs = data.get(g)
                    if pairs and any(v is not None for _, v in pairs):
                        r.add(sp, pairs)
                        break
                else:
                    err = geo_errors.get(str(sp.req["code"]))
                    # counted in report["failed"] -> the job ends `partial`, never a silent success
                    r.fail(sp, f"fetch failed for geo {sp.req['code']} in {ds}: {err}" if err
                           else f"no data for geo {sp.req['code']} in {ds}")
        r.attempt(f"eurostat:{ds}", one, grp)


def eurostat_since(start: date, freq: str) -> str:
    if freq == "Q":
        return f"{start.year}-Q1"
    if freq == "A":
        return str(start.year)
    return start.strftime("%Y-%m")


@fetcher("bundesbank")
def fetch_bundesbank(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: key 'FLOW/KEY' (Bundesbank SDMX REST, csv)."""
    for sp in specs:
        def one(sp: GSeries = sp) -> None:
            flow, key = str(sp.req["key"]).split("/", 1)
            resp = r.http("bundesbank").get(f"https://api.statistiken.bundesbank.de/rest/data/{flow}/{key}",
                                            {"startPeriod": r.since("%Y-%m-%d"), "format": "csv", "lang": "en"},
                                            cache_ttl=6 * 3600, headers={"Accept": "text/csv"})
            r.add(sp, parse_bbk_csv(resp.text))
        r.attempt(sp.series_id, one, [sp])


def parse_bbk_csv(text: str) -> list[tuple[date, float | None]]:
    """Bundesbank csv: SDMX-CSV (TIME_PERIOD/OBS_VALUE) or the classic 'date;value;flag' layout."""
    from app.ingestion.macro_common import parse_sdmx_keyed  # noqa: PLC0415

    keyed = parse_sdmx_keyed(text)
    if keyed:
        return sorted(p for v in keyed.values() for p in v)
    out = []
    for row in csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";" if ";" in text[:2000] else ","):
        if len(row) < 2:
            continue
        try:
            d = parse_period(row[0].strip())
        except ValueError:
            continue
        out.append((d, to_float(row[1].replace(",", ".") if row[1].count(",") == 1 and "." not in row[1] else row[1])))
    return sorted(out)


@fetcher("snb")
def fetch_snb(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: cube, dims {dimId: value} -> SNB data portal CSV (semicolon; 'Date;D0;D1;Value')."""
    groups: dict[str, list[GSeries]] = {}
    for sp in specs:
        groups.setdefault(sp.req["cube"], []).append(sp)
    for cube, grp in groups.items():
        def one(cube: str = cube, grp: list[GSeries] = grp) -> None:
            sel = snb_dimsel(grp)
            params = {"fromDate": r.since("%Y-%m-%d") if grp[0].frequency == "D" else r.since("%Y-%m")}
            if sel:
                params["dimSel"] = sel
            resp = r.http("snb").get(f"https://data.snb.ch/api/cube/{cube}/data/csv/en", params, cache_ttl=6 * 3600)
            rows = parse_snb_csv(resp.text)
            for sp in grp:
                want = {str(k): str(v) for k, v in (sp.req.get("dims") or {}).items()}
                pairs = sorted((d, v) for d, dims, v in rows if all(dims.get(k) == x for k, x in want.items()))
                if pairs:
                    r.add(sp, pairs)
                else:
                    r.fail(sp, f"dims {want} not in SNB cube {cube}")
        r.attempt(f"snb:{cube}", one, grp)


def snb_dimsel(grp: list[GSeries]) -> str:
    """dimSel=D0(a,b),D1(c) covering every requested combination (over-fetch is filtered afterwards)."""
    dims: dict[str, list[str]] = {}
    for sp in grp:
        for k, v in (sp.req.get("dims") or {}).items():
            dims.setdefault(str(k), [])
            if str(v) not in dims[str(k)]:
                dims[str(k)].append(str(v))
    return ",".join(f"{k}({','.join(v)})" for k, v in sorted(dims.items()))


def parse_snb_csv(text: str) -> list[tuple[date, dict[str, str], float | None]]:
    lines = text.lstrip("\ufeff").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.replace('"', "").startswith("Date;")), None)
    if start is None:
        return []
    rdr = csv.reader(io.StringIO("\n".join(lines[start:])), delimiter=";")
    header = next(rdr)
    out = []
    for row in rdr:
        if len(row) != len(header):
            continue
        try:
            d = parse_period(row[0])
        except ValueError:
            continue
        dims = {header[i]: row[i] for i in range(1, len(header) - 1)}
        out.append((d, dims, to_float(row[-1])))
    return out


# ------------------------------------------------------------------------------------------------ national EU


@fetcher("insee")
def fetch_insee(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: idbank (INSEE BDM SDMX, StructureSpecificData XML); up to 50 idbanks '+'-joined per request."""
    for i in range(0, len(specs), 50):
        chunk = specs[i:i + 50]

        def one(chunk: list[GSeries] = chunk) -> None:
            ids = "+".join(str(sp.req["idbank"]) for sp in chunk)
            resp = r.http("insee").get(f"https://bdm.insee.fr/series/sdmx/data/SERIES_BDM/{ids}",
                                       {"startPeriod": r.since("%Y")}, cache_ttl=6 * 3600)
            data = parse_insee_xml(resp.content)
            for sp in chunk:
                pairs = data.get(str(sp.req["idbank"]))
                if pairs:
                    r.add(sp, pairs)
                else:
                    r.fail(sp, f"idbank {sp.req['idbank']} not in INSEE response")
        r.attempt("insee", one, chunk)


def parse_insee_xml(content: bytes) -> dict[str, list[tuple[date, float | None]]]:
    from lxml import etree  # noqa: PLC0415

    root = etree.fromstring(content)
    out: dict[str, list[tuple[date, float | None]]] = {}
    for s in root.iter():
        if not isinstance(s.tag, str) or not s.tag.endswith("Series"):
            continue
        rows = []
        for o in s:
            if isinstance(o.tag, str) and o.tag.endswith("Obs"):
                try:
                    rows.append((parse_period(o.get("TIME_PERIOD", "")), to_float(o.get("OBS_VALUE"))))
                except ValueError:
                    continue
        out[s.get("IDBANK") or ""] = sorted(rows)
    return out


@fetcher("ine_es")
def fetch_ine_es(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: cod (INE Tempus series code)."""
    for sp in specs:
        def one(sp: GSeries = sp) -> None:
            n = max(12, (date.today().year - r.start.year + 1) * {"M": 12, "Q": 4, "A": 1}.get(sp.frequency, 12))
            j = r.http("ine_es").get(f"https://servicios.ine.es/wstempus/js/EN/DATOS_SERIE/{sp.req['cod']}",
                                     {"nult": n}, cache_ttl=6 * 3600).json()
            r.add(sp, parse_ine_es(j))
        r.attempt(sp.series_id, one, [sp])


def parse_ine_es(j: dict[str, Any]) -> list[tuple[date, float | None]]:
    """Use Anyo + FK_Periodo (1..12 months; 19..22 quarters; else annual) - never the local-midnight epoch Fecha."""
    out = []
    for x in j.get("Data", []) or []:
        y, p = int(x["Anyo"]), int(x.get("FK_Periodo") or 0)
        if 1 <= p <= 12:
            d = date(y, p, 1)
        elif 19 <= p <= 22:
            d = date(y, (p - 19) * 3 + 1, 1)
        else:
            d = date(y, 1, 1)
        out.append((d, to_float(x.get("Valor"))))
    return sorted(out)


@fetcher("bpstat")
def fetch_bpstat(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: domain, dataset, sid (BPstat series id). One request per dataset with all series ids."""
    groups: dict[tuple[str, str], list[GSeries]] = {}
    for sp in specs:
        groups.setdefault((str(sp.req["domain"]), str(sp.req["dataset"])), []).append(sp)
    for (dom, ds), grp in groups.items():
        def one(dom: str = dom, ds: str = ds, grp: list[GSeries] = grp) -> None:
            j = r.http("bpstat").get(f"https://bpstat.bportugal.pt/data/v1/domains/{dom}/datasets/{ds}/",
                                     {"lang": "EN", "series_ids": ",".join(str(sp.req["sid"]) for sp in grp),
                                      "obs_since": r.since("%Y-%m-%d")}, cache_ttl=6 * 3600).json()
            data = parse_bpstat(j)
            for sp in grp:
                pairs = data.get(str(sp.req["sid"]))
                if pairs:
                    r.add(sp, pairs)
                else:
                    r.fail(sp, f"series {sp.req['sid']} not in BPstat response")
        r.attempt(f"bpstat:{ds}", one, grp)


def parse_bpstat(j: dict[str, Any]) -> dict[str, list[tuple[date, float | None]]]:
    """BPstat JSON-stat 2.0: extension.series[] gives each series' category per dimension; row-major values."""
    ids, size = [str(x) for x in j["id"]], j["size"]
    strides = [1] * len(ids)
    for i in range(len(ids) - 2, -1, -1):
        strides[i] = strides[i + 1] * size[i + 1]
    idx = {d: {str(c): n for n, c in enumerate(j["dimension"][d]["category"]["index"])} for d in ids}
    tdim = "reference_date" if "reference_date" in ids else ids[-1]
    ti = ids.index(tdim)
    times = list(j["dimension"][tdim]["category"]["index"])
    vals = j.get("value", [])
    out: dict[str, list[tuple[date, float | None]]] = {}
    for s in (j.get("extension") or {}).get("series", []):
        cats = {str(dc["dimension_id"]): str(dc["category_id"]) for dc in s.get("dimension_category", [])}
        try:
            base = sum(idx[d][cats[d]] * strides[k] for k, d in enumerate(ids) if d != tdim)
        except KeyError:
            continue
        rows = []
        for n, t in enumerate(times):
            flat = base + n * strides[ti]
            v = vals.get(str(flat)) if isinstance(vals, dict) else (vals[flat] if flat < len(vals) else None)
            rows.append((parse_period(t), to_float(v)))
        out[str(s["id"])] = sorted(rows)
    return out


def run(ctx):  # noqa: ANN001 - JobContext
    return run_job(ctx, "macro_eu", SOURCES)
