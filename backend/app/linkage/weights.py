"""A7 spatial weight matrices (job `linkage_weights`) -> spatial_weights, plus the helper API get_W().

Stored long (one row per non-zero cell): kind, level, period, as_of (first date the matrix may be used, PIT), version,
i, j, w_raw, w_row (row-standardized: sum_j w_row = 1 for every row with neighbours). Kinds:

  level country (ISO2; Natural Earth countries):
    contiguity_queen / contiguity_rook  binary, symmetric (Natural Earth 10m polygons, libpysal)        period static
    inv_distance                         1 / great-circle distance in 1000 km (label points), symmetric    period static
    knn5                                 1 for the 5 nearest countries (not symmetric)                     period static
    trade                                total bilateral goods trade (X+M, both reporters' views averaged,
                                         mirror-filled), symmetric, IMF IMTS (Comtrade fallback)          period YYYY
    trade_exports                        i's exports to j (i's demand exposure to j), directional          period YYYY
    io_upstream                          intermediate inputs bought by i from j (OECD ICIO), directional  period YYYY
    bank_claims                          cross-border bank claims of i on j (BIS LBS; for non-reporting i the
                                         LBS liabilities of j's banks to i), directional                  period YYYY (Q4)
  level country_sector ('US:TECH'):
    io_upstream                          ICIO intermediate inputs bought by i from j (latest year only)   period YYYY
  level issuer (issuer_id):
    customer                             supplier i -> customer j, revenue share (company_links)          period YYYY
    comention                            GDELT co-mentions, symmetric, trailing 12 months                 period YYYY-MM
  level issuer_country (bipartite, i = issuer_id, j = ISO2):
    exposure                             revenue share by country from the latest annual geographic
                                         breakdown known at as_of (regions / rest-of-world spread over member
                                         countries by Natural Earth GDP, excluding separately reported ones)  period YYYY

get_W(kind, level='country', as_of=None, standardize='r', fmt='W', ids=None, period=None, version=None)
  -> libpysal.weights.W (fmt 'W'), or (ids, scipy.sparse.csr_matrix) (fmt 'sparse'); bipartite 'exposure' returns
  (row_ids, col_ids, csr). as_of picks the latest period with as_of <= the date (static kinds ignore it).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings
from app.linkage.common import NOT_COUNTRIES, log, now_utc, settings_of, update_progress, write

TABLE = "spatial_weights"
KNN_K = 5
SQUARE_LEVELS = {"country", "country_sector", "issuer"}
KEEP_VERSIONS = 3


# ================================================================================================ helpers
def _frame(kind: str, level: str, period: str, as_of: date, i: list[str], j: list[str], w: np.ndarray | list[float],
           version: str) -> pl.DataFrame:
    df = pl.DataFrame({"i": i, "j": j, "w_raw": np.asarray(w, dtype=float)}).filter(
        (pl.col("w_raw") > 0) & pl.col("w_raw").is_finite() & (pl.col("i") != pl.col("j")))
    df = df.group_by("i", "j").agg(pl.col("w_raw").sum())
    df = df.with_columns((pl.col("w_raw") / pl.col("w_raw").sum().over("i")).alias("w_row"))
    return df.with_columns(pl.lit(kind).alias("kind"), pl.lit(level).alias("level"), pl.lit(period).alias("period"),
                           pl.lit(as_of).alias("as_of"), pl.lit(version).alias("version"))


def _year_asof(y: int) -> date:
    """Annual data of year y usable from y+1-06-30 (publication-lag rule, as trade_bilateral.knowledge_date)."""
    return date(y + 1, 6, 30)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    import calendar  # noqa: PLC0415

    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _read(table: str, settings: Settings | None, columns: list[str] | None = None, filters: pl.Expr | None = None):
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        return read_table(table, columns=columns, filters=filters, settings=settings)
    except Exception as e:  # noqa: BLE001
        log.warning("spatial_weights: %s unavailable (%s)", table, e)
        return pl.DataFrame()


# ================================================================================================ builders
def build_geo(settings: Settings | None, version: str) -> list[pl.DataFrame]:
    from app.linkage.geo import distance_matrix  # noqa: PLC0415

    geo = _read("geo_countries", settings)
    if geo.is_empty():
        return []
    static = date(2000, 1, 1)
    out = []
    for kind, col in (("contiguity_queen", "neighbors_queen"), ("contiguity_rook", "neighbors_rook")):
        e = geo.select("iso2", col).explode(col, empty_as_null=True).drop_nulls()
        out.append(_frame(kind, "country", "static", static, e["iso2"].to_list(), e[col].to_list(),
                          np.ones(e.height), version))
    ids, d = distance_matrix(geo)
    n = len(ids)
    ii, jj = np.where(~np.eye(n, dtype=bool))
    dist = d[ii, jj]
    out.append(_frame("inv_distance", "country", "static", static, [ids[k] for k in ii], [ids[k] for k in jj],
                      1000.0 / np.maximum(dist, 1.0), version))
    dd = d + np.eye(n) * 1e12
    nn = np.argsort(dd, axis=1)[:, :KNN_K]
    out.append(_frame("knn5", "country", "static", static, [ids[a] for a in range(n) for _ in range(KNN_K)],
                      [ids[b] for a in range(n) for b in nn[a]], np.ones(n * KNN_K), version))
    return out


def trade_matrix(t: pl.DataFrame) -> pl.DataFrame:
    """Per (year, i, j): sym = mean of available views of total trade; exports_ij (mirror: j's imports from i)."""
    ok = [c for c in ("reporter_iso2", "partner_iso2")]
    t = t.filter((pl.col("cmd") == "TOTAL") & pl.all_horizontal(
        [pl.col(c).str.contains(r"^[A-Z]{2}$") & ~pl.col(c).is_in(sorted(NOT_COUNTRIES)) for c in ok]))
    # prefer IMF; Comtrade fills reporter-years IMF lacks
    t = t.with_columns(pl.when(pl.col("source") == "imf_imts").then(0).otherwise(1).alias("__p")).sort("__p").unique(
        ["reporter_iso2", "partner_iso2", "year", "flow"], keep="first").drop("__p")
    x = t.filter(pl.col("flow") == "X").select("year", pl.col("reporter_iso2").alias("i"),
                                                 pl.col("partner_iso2").alias("j"), pl.col("value_usd").alias("x_rep"))
    m = t.filter(pl.col("flow") == "M").select("year", pl.col("partner_iso2").alias("i"),
                                                 pl.col("reporter_iso2").alias("j"), pl.col("value_usd").alias("x_mirror"))
    ex = x.join(m, on=["year", "i", "j"], how="full", coalesce=True).with_columns(
        pl.coalesce("x_rep", pl.col("x_mirror") / 1.06).alias("exports"))  # CIF -> FOB ~6% (IMF DOTS convention)
    a = ex.select("year", "i", "j", "exports")
    b = a.select("year", pl.col("j").alias("i"), pl.col("i").alias("j"), pl.col("exports").alias("imports"))
    tot = a.join(b, on=["year", "i", "j"], how="full", coalesce=True).with_columns(
        (pl.col("exports").fill_null(0) + pl.col("imports").fill_null(0)).alias("total"))
    return tot


def build_trade(settings: Settings | None, version: str, start: int = 2000) -> list[pl.DataFrame]:
    t = _read("trade_bilateral", settings, columns=["source", "reporter_iso2", "partner_iso2", "year", "flow", "cmd",
                                                     "value_usd"])
    if t.is_empty():
        return []
    tot = trade_matrix(t.filter(pl.col("year") >= start))
    out = []
    for (y,), g in tot.group_by("year"):
        y = int(y)
        out.append(_frame("trade", "country", str(y), _year_asof(y), g["i"].to_list(), g["j"].to_list(),
                          g["total"].to_numpy(), version))
        e = g.filter(pl.col("exports").is_not_null())
        out.append(_frame("trade_exports", "country", str(y), _year_asof(y), e["i"].to_list(), e["j"].to_list(),
                          e["exports"].to_numpy(), version))
    return out


def build_io(settings: Settings | None, version: str) -> list[pl.DataFrame]:
    f = _read("io_flows", settings)
    if f.is_empty():
        return []
    f = f.filter(pl.col("dst_sector") != "FD").sort("edition").unique(
        ["year", "src_iso2", "src_sector", "dst_iso2", "dst_sector"], keep="last")
    out = []
    last = int(f["year"].max())
    for (y,), g in f.group_by("year"):
        y = int(y)
        ed = int(g["edition"].max())
        ka = date(ed, 11, 30)
        c = g.group_by("dst_iso2", "src_iso2").agg(pl.col("value_usd_m").sum())
        out.append(_frame("io_upstream", "country", str(y), ka, c["dst_iso2"].to_list(), c["src_iso2"].to_list(),
                          c["value_usd_m"].to_numpy(), version))
        if y == last:
            cs = g.filter((pl.col("src_sector") != "NONMKT") & (pl.col("dst_sector") != "NONMKT")).with_columns(
                (pl.col("dst_iso2") + ":" + pl.col("dst_sector")).alias("i"),
                (pl.col("src_iso2") + ":" + pl.col("src_sector")).alias("j"))
            out.append(_frame("io_upstream", "country_sector", str(y), ka, cs["i"].to_list(), cs["j"].to_list(),
                              cs["value_usd_m"].to_numpy(), version))
    return out


def build_bank(settings: Settings | None, version: str) -> list[pl.DataFrame]:
    b = _read("bank_claims", settings, filters=pl.col("dataset").is_in(["lbs_claims", "lbs_liabilities"]))
    if b.is_empty():
        return []
    last_p = b["period"].max()
    b = b.filter(pl.col("period").str.ends_with("Q4") | (pl.col("period") == last_p))
    cl = b.filter(pl.col("dataset") == "lbs_claims").select("period", "period_end", pl.col("reporter_iso2").alias("i"),
                                                            pl.col("counterparty_iso2").alias("j"), "value_usd_m")
    li = b.filter(pl.col("dataset") == "lbs_liabilities").select(
        "period", "period_end", pl.col("counterparty_iso2").alias("i"), pl.col("reporter_iso2").alias("j"),
        pl.col("value_usd_m").alias("mirror"))
    reporters = set(cl["i"].unique().to_list())
    m = cl.join(li, on=["period", "period_end", "i", "j"], how="full", coalesce=True).with_columns(
        pl.when(pl.col("i").is_in(sorted(reporters))).then(pl.col("value_usd_m")).otherwise(pl.col("mirror")).alias("w"))
    out = []
    for (p,), g in m.filter(pl.col("w").is_not_null()).group_by("period"):
        pe = g["period_end"].max()
        label = p[:4] if p.endswith("Q4") and p != last_p else p
        out.append(_frame("bank_claims", "country", label, _add_months(pe, 4), g["i"].to_list(), g["j"].to_list(),
                          g["w"].to_numpy(), version))
    return out


def build_customer(settings: Settings | None, version: str) -> list[pl.DataFrame]:
    c = _read("company_links", settings, filters=pl.col("link_type").is_in(["customer", "supplier"]))
    if c.is_empty():
        return []
    c = c.filter(pl.col("src_issuer_id").is_not_null() & pl.col("dst_issuer_id").is_not_null())
    # roadmap #9 reverse edges: a supplier disclosed by its customer (src = customer, dst = supplier) is the edge
    # supplier -> customer with an unknown revenue share (0.05 assumed: below the >= 10% disclosure threshold)
    sup = c.filter(pl.col("link_type") == "supplier").with_columns(
        pl.col("dst_issuer_id").alias("__s"), pl.col("src_issuer_id").alias("__c"), pl.lit(0.05).alias("share"))
    cus = c.filter(pl.col("link_type") == "customer").with_columns(
        pl.col("src_issuer_id").alias("__s"), pl.col("dst_issuer_id").alias("__c"))
    c = pl.concat([cus, sup], how="diagonal_relaxed").with_columns(
        pl.col("__s").alias("src_issuer_id"), pl.col("__c").alias("dst_issuer_id"),
        pl.col("period_end").dt.year().alias("y")).drop("__s", "__c")
    out = []
    # year Y matrix = latest disclosure per pair among fiscal periods ending in Y-1 or Y (customers are disclosed
    # annually; a trailing 2-year window keeps the current, partial year complete). as_of = latest filing used.
    for y in sorted(c["y"].unique().to_list()):
        g = c.filter(pl.col("y").is_between(y - 1, y)).with_columns((pl.col("link_type") == "customer").alias("__p")).sort(["period_end", "knowledge_date", "__p"]).unique(
            ["src_issuer_id", "dst_issuer_id"], keep="last")
        out.append(_frame("customer", "issuer", str(int(y)), g["knowledge_date"].max(), g["src_issuer_id"].to_list(),
                          g["dst_issuer_id"].to_list(), g["share"].fill_null(0.1).to_numpy(), version))
    return out


def build_comention(settings: Settings | None, version: str) -> list[pl.DataFrame]:
    c = _read("company_links", settings, filters=pl.col("link_type") == "co_mention")
    if c.is_empty():
        return []
    months = sorted(c["period_end"].unique().to_list())
    out = []
    for m in months:
        lo = date(m.year - 1, m.month, 28) if m.month != 2 else date(m.year - 1, 2, 28)
        g = c.filter((pl.col("period_end") > lo) & (pl.col("period_end") <= m)).group_by(
            "src_issuer_id", "dst_issuer_id").agg(pl.col("n_mentions").sum())
        g = pl.concat([g, g.select(pl.col("dst_issuer_id").alias("src_issuer_id"),
                                   pl.col("src_issuer_id").alias("dst_issuer_id"), "n_mentions")])
        out.append(_frame("comention", "issuer", m.strftime("%Y-%m"), m, g["src_issuer_id"].to_list(),
                          g["dst_issuer_id"].to_list(), g["n_mentions"].cast(pl.Float64).to_numpy(), version))
    return out


def allocate_exposure(rows: pl.DataFrame, gdp: dict[str, float], settings: Settings | None = None) -> dict[str, float]:
    """One filing-period's geographic revenue rows -> {iso2: share} (sums to 1)."""
    from app.linkage.members import region_countries  # noqa: PLC0415

    rows = rows.filter(pl.col("member_type") != "other").filter(pl.col("value") > 0)
    if rows.is_empty():
        return {}
    explicit = {r for r in rows.filter(pl.col("member_type").is_in(["country", "domestic"]))["iso2"].drop_nulls().to_list()}
    out: dict[str, float] = {}
    for t, iso, reg, v in rows.select("member_type", "iso2", "region", "value").iter_rows():
        if t in ("country", "domestic"):
            if iso:
                out[iso] = out.get(iso, 0.0) + v
            continue
        pool = region_countries(reg, settings) if t == "region" and reg else list(gdp)
        pool = [c for c in pool if c not in explicit and gdp.get(c)]
        tot = sum(gdp[c] for c in pool)
        if tot <= 0:
            continue
        for c in pool:
            out[c] = out.get(c, 0.0) + v * gdp[c] / tot
    s = sum(out.values())
    return {k: v / s for k, v in out.items() if v > 0} if s > 0 else {}


def build_exposure(settings: Settings | None, version: str, start: int = 2009) -> list[pl.DataFrame]:
    g = _read("company_geo_revenue", settings, filters=(pl.col("measure") == "revenue") & (pl.col("qtrs") == 4)
              & pl.col("issuer_id").is_not_null())
    geo = _read("geo_countries", settings, columns=["iso2", "gdp_md_usd"])
    if g.is_empty() or geo.is_empty():
        return []
    gdp = {a: float(b) for a, b in geo.drop_nulls().iter_rows() if b and b > 0}
    g = g.with_columns(pl.col("period_end").dt.year().alias("fy")).filter(pl.col("fy") >= start)
    # one breakdown per issuer & fiscal year: latest filing (restatements win), most country detail
    best = (g.group_by("issuer_id", "fy", "filing_id", "concept").agg(pl.col("knowledge_date").max(),
                                                                       (pl.col("member_type") == "country").sum().alias("nc"))
            .sort(["knowledge_date", "nc"], descending=[True, True]).unique(["issuer_id", "fy"], keep="first"))
    g = g.join(best.select("issuer_id", "fy", "filing_id", "concept"), on=["issuer_id", "fy", "filing_id", "concept"])
    rows_i, rows_j, rows_w, rows_y, rows_k = [], [], [], [], []
    for (iid, fy), grp in g.group_by("issuer_id", "fy"):
        sh = allocate_exposure(grp, gdp, settings)
        kd = grp["knowledge_date"].max()
        for c, w in sh.items():
            rows_i.append(iid)
            rows_j.append(c)
            rows_w.append(w)
            rows_y.append(int(fy))
            rows_k.append(kd)
    if not rows_i:
        return []
    df = pl.DataFrame({"i": rows_i, "j": rows_j, "w": rows_w, "fy": rows_y, "kd": rows_k})
    out = []
    for (fy,), gg in df.group_by("fy"):
        f = _frame("exposure", "issuer_country", str(int(fy)), gg["kd"].max(), gg["i"].to_list(), gg["j"].to_list(),
                   gg["w"].to_numpy(), version)
        out.append(f)
    return out


BUILDERS = {"geo": build_geo, "trade": build_trade, "io": build_io, "bank": build_bank, "customer": build_customer,
            "comention": build_comention, "exposure": build_exposure}


def build_weights(settings: Settings | None = None, parts: list[str] | None = None,
                  version: str | None = None) -> dict[str, Any]:
    s = settings_of(settings)
    version = version or now_utc().strftime("%Y%m%d")
    rep: dict[str, Any] = {"version": version}
    for name in parts or list(BUILDERS):
        frames = [f for f in BUILDERS[name](s, version) if not f.is_empty()]
        if not frames:
            rep[name] = 0
            continue
        df = pl.concat(frames, how="vertical_relaxed").with_columns(pl.lit(now_utc()).alias("built_at"))
        rep[name] = replace_version(df, version, s)
        rep[f"{name}_matrices"] = df.select("kind", "level", "period").n_unique()
    rep["pruned"] = prune_versions(s)
    log.info("spatial_weights: %s", rep)
    return rep


def replace_version(df: pl.DataFrame, version: str, settings: Settings | None = None) -> int:
    """Write the new matrices of `version`, replacing that version's previous rows of the same (kind, level) - a
    same-day rebuild must not leave stale cells (row sums would drift from 1); other versions are kept."""
    keys = df.select("kind", "level").unique()
    old = _read(TABLE, settings, filters=pl.col("kind").is_in(keys["kind"].to_list()))
    if not old.is_empty():
        stale = old.join(keys, on=["kind", "level"], how="semi").filter(pl.col("version") == version)
        if stale.height:
            keep = old.join(stale.select("kind", "level", "version").unique(), on=["kind", "level", "version"],
                            how="anti")
            df = pl.concat([keep.select(df.columns), df], how="vertical_relaxed")
            write(df, TABLE, settings, mode="overwrite_partition")
            return df.filter(pl.col("version") == version).height
    return write(df, TABLE, settings, mode="upsert")


def prune_versions(settings: Settings | None = None, keep: int = KEEP_VERSIONS) -> int:
    df = _read(TABLE, settings, columns=["version"])
    if df.is_empty():
        return 0
    vs = sorted(df["version"].unique().to_list())
    if len(vs) <= keep:
        return 0
    full = _read(TABLE, settings).filter(pl.col("version").is_in(vs[-keep:]))
    write(full, TABLE, settings, mode="overwrite")
    return len(vs) - keep


# ================================================================================================ API
def _as_date(d: Any) -> date | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d)[:10])


def list_W(settings: Settings | None = None) -> pl.DataFrame:
    """Available matrices: kind, level, period, as_of, version, n_rows, n_cells."""
    df = _read(TABLE, settings, columns=["kind", "level", "period", "as_of", "version", "i"])
    if df.is_empty():
        return df
    return df.group_by("kind", "level", "period", "version").agg(
        pl.col("as_of").max(), pl.col("i").n_unique().alias("n_rows"), pl.len().alias("n_cells")).sort(
        "kind", "level", "period")


def get_W(kind: str, level: str = "country", as_of: Any = None, standardize: str = "r", fmt: str = "W",
          ids: list[str] | None = None, period: str | None = None, version: str | None = None,
          settings: Settings | None = None) -> Any:
    """Spatial weights for the event-models / Model Studio agents.

    kind / level: see the module docstring (list_W() shows what is built).
    as_of: date | 'YYYY-MM-DD' | None (latest). Picks the latest period whose as_of <= the date (PIT); static kinds
           ignore it. period / version pin a specific matrix.
    standardize: 'r' row-standardized (stored w_row, or re-standardized over `ids` when a subset is requested),
                 'b' / 'raw' raw weights (binary for contiguity / knn).
    fmt: 'W' -> libpysal.weights.W (square levels), 'sparse' -> (ids, scipy.sparse.csr_matrix);
         'frame' -> the long polars frame. Level issuer_country (bipartite) always returns
         (row_ids, col_ids, csr) for fmt 'W' / 'sparse'.
    ids: optional ordering / subset of node ids (rows and columns; for the bipartite level: the row ids). Nodes in
         ids without data get an empty row (an island).
    """
    from scipy import sparse  # noqa: PLC0415

    s = settings_of(settings)
    f = (pl.col("kind") == kind) & (pl.col("level") == level)
    df = _read(TABLE, s, filters=f)
    if df.is_empty():
        raise KeyError(f"no spatial_weights for kind={kind!r} level={level!r} (see list_W())")
    v = version or df["version"].max()
    df = df.filter(pl.col("version") == v)
    if period is not None:
        df = df.filter(pl.col("period") == str(period))
    elif (d := _as_date(as_of)) is not None and df["period"].first() != "static":
        ok = df.filter(pl.col("as_of") <= d)
        if ok.is_empty():
            raise KeyError(f"no {kind}/{level} matrix known at {d} (earliest as_of {df['as_of'].min()})")
        df = ok
    if df["period"].n_unique() > 1:
        pick = df.group_by("period").agg(pl.col("as_of").max()).sort(["as_of", "period"]).row(-1)[0]
        df = df.filter(pl.col("period") == pick)
    raw = standardize in ("b", "raw", "B")
    if level == "issuer_country":
        rows = ids or sorted(df["i"].unique().to_list())
        cols = sorted(df["j"].unique().to_list())
        df = df.filter(pl.col("i").is_in(rows))
        ri, ci = {k: n for n, k in enumerate(rows)}, {k: n for n, k in enumerate(cols)}
        w = df["w_raw" if raw else "w_row"].to_numpy()
        m = sparse.csr_matrix((w, ([ri[x] for x in df["i"].to_list()], [ci[x] for x in df["j"].to_list()])),
                              shape=(len(rows), len(cols)))
        return df if fmt == "frame" else (rows, cols, m)
    nodes = ids or sorted(set(df["i"].to_list()) | set(df["j"].to_list()))
    if ids is None and level == "country" and kind.startswith("contiguity"):  # keep islands as isolates
        geo = _read("geo_countries", s, columns=["iso2"])
        nodes = sorted(set(nodes) | set(geo["iso2"].to_list())) if not geo.is_empty() else nodes
    sub = df.filter(pl.col("i").is_in(nodes) & pl.col("j").is_in(nodes))
    if ids is not None and not raw:  # re-standardize over the requested subset
        sub = sub.with_columns((pl.col("w_raw") / pl.col("w_raw").sum().over("i")).alias("w_row"))
    if fmt == "frame":
        return sub
    col = "w_raw" if raw else "w_row"
    if fmt == "sparse":
        ix = {k: n for n, k in enumerate(nodes)}
        m = sparse.csr_matrix((sub[col].to_numpy(), ([ix[x] for x in sub["i"].to_list()],
                                                     [ix[x] for x in sub["j"].to_list()])), shape=(len(nodes), len(nodes)))
        return nodes, m
    from libpysal.weights import W  # noqa: PLC0415

    neigh: dict[str, list[str]] = {k: [] for k in nodes}
    wts: dict[str, list[float]] = {k: [] for k in nodes}
    for a, b, w in sub.select("i", "j", col).iter_rows():
        neigh[a].append(b)
        wts[a].append(float(w))
    out = W(neigh, wts, id_order=list(nodes), silence_warnings=True)
    if not raw:
        out.transform = "r"  # already row-standardized; sets the libpysal flag (idempotent)
    return out


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    parts = [x.strip() for x in str(p["parts"]).split(",")] if p.get("parts") else None
    rep = build_weights(ctx.settings, parts=parts)
    n = sum(v for k, v in rep.items() if k in BUILDERS and isinstance(v, int))
    update_progress("A7", "done" if n else "partial", settings=ctx.settings, **rep)
    return JobResult(status="success" if n else "partial", rows_written={TABLE: n}, message=str(rep))
