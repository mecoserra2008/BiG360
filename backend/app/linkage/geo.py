"""A1 geography (job `linkage_geo`): Natural Earth admin-0 (public domain) -> geo_countries.

Source: https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip (official Natural Earth CDN,
~5 MB, v5.1.x). Read with geopandas/pyogrio; one row per ISO2 (ISO_A2, else ISO_A2_EH; '-99' parts such as
Somaliland / N. Cyprus are dissolved into their sovereign when it exists, otherwise dropped).
  * lat / lon        Natural Earth label point (inside the main landmass; used for distances)
  * centroid_*       area centroid of the largest polygon (equal-area projection EPSG:6933)
  * neighbors_queen  libpysal Queen contiguity on the 10m polygons (shared vertex); neighbors_rook = shared edge
  * pop_est / gdp_md Natural Earth attributes (used to spread regional revenue over countries, A7 exposure)
Distances (great-circle, km, between label points) are computed on demand by distance_matrix().
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings
from app.linkage.common import log, now_utc, raw_dir, settings_of, update_progress, write

NE_URL = "https://naciscdn.org/naturalearth/10m/cultural/ne_10m_admin_0_countries.zip"
EARTH_R_KM = 6371.0088
#: Natural Earth admin-0 units without an ISO2 -> sovereign ISO2 (dissolved into it)
NE_DISSOLVE = {"Somaliland": "SO", "N. Cyprus": "CY", "Cyprus U.N. Buffer Zone": "CY", "Baikonur": "KZ",
               "Siachen Glacier": "IN"}


def download(settings: Settings | None = None, force: bool = False):
    from app.linkage.common import http  # noqa: PLC0415

    s = settings_of(settings)
    dest = raw_dir("natural_earth", s) / "ne_10m_admin_0_countries.zip"
    if force or not dest.exists():
        with http("natural_earth", s) as cl:
            cl.download(NE_URL, dest)
    return dest


def _iso2(r: dict[str, Any]) -> str | None:
    for f in ("ISO_A2", "ISO_A2_EH", "WB_A2"):
        v = r.get(f)
        if isinstance(v, str) and len(v) == 2 and v.isalpha():
            return v.upper()
    return NE_DISSOLVE.get(r.get("NAME") or r.get("ADMIN"))


def read_countries(zip_path) -> Any:
    """GeoDataFrame, one row per ISO2, with Natural Earth attributes."""
    import geopandas as gpd  # noqa: PLC0415

    gdf = gpd.read_file(f"zip://{zip_path}")
    gdf.columns = [c.upper() if c != "geometry" else c for c in gdf.columns]
    gdf["iso2"] = [_iso2(r) for r in gdf.drop(columns="geometry").to_dict("records")]
    gdf = gdf[gdf["iso2"].notna()].copy()
    # dissolve duplicates (sovereign absorbs '-99' parts); keep the attributes of the largest-population unit
    gdf["__pop"] = gdf["POP_EST"].fillna(0)
    gdf = gdf.sort_values("__pop", ascending=False)
    attrs = gdf.drop(columns="geometry").drop_duplicates("iso2", keep="first").set_index("iso2")
    geom = gdf.dissolve(by="iso2")[["geometry"]]
    out = geom.join(attrs.drop(columns=["__pop"]), how="left").reset_index()
    return out.set_crs(gdf.crs) if out.crs is None else out


def _ival(v: Any) -> int | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) or f < -90 else int(f)


def build_frame(gdf: Any, version: str | None = None) -> pl.DataFrame:
    """geo_countries rows (+ queen / rook contiguity) from the dissolved GeoDataFrame."""
    from libpysal.weights import Queen, Rook  # noqa: PLC0415

    ids = list(gdf["iso2"])
    q = Queen.from_dataframe(gdf, ids=ids, use_index=False, silence_warnings=True)
    r = Rook.from_dataframe(gdf, ids=ids, use_index=False, silence_warnings=True)
    ea = gdf.to_crs(6933)
    area = ea.geometry.area / 1e6
    largest = ea.geometry.apply(lambda g: max(getattr(g, "geoms", [g]), key=lambda p: p.area))
    cen = largest.centroid.set_crs(6933).to_crs(4326)
    rows = []
    for i, rec in enumerate(gdf.drop(columns="geometry").to_dict("records")):
        iso = rec["iso2"]
        alt = {str(rec.get(f)) for f in ("ADMIN", "NAME_SORT", "NAME_CIAWF", "NAME_EN", "NAME_ALT", "GEOUNIT", "SOVEREIGNT")
               if rec.get(f) and str(rec.get(f)) != "None"}
        iso3 = rec.get("ISO_A3") if isinstance(rec.get("ISO_A3"), str) and rec["ISO_A3"] != "-99" else rec.get("ISO_A3_EH")
        n3 = rec.get("ISO_N3") if rec.get("ISO_N3") not in (None, "-99") else rec.get("ISO_N3_EH")
        rows.append({
            "iso2": iso, "iso3": iso3 if iso3 and iso3 != "-99" else None,
            "iso_n3": str(n3).zfill(3) if n3 not in (None, "-99") else None,
            "name": rec.get("NAME"), "name_long": rec.get("NAME_LONG"), "formal_name": rec.get("FORMAL_EN"),
            "alt_names": sorted(alt - {rec.get("NAME")}), "continent": rec.get("CONTINENT"),
            "subregion": rec.get("SUBREGION"), "region_un": rec.get("REGION_UN"), "region_wb": rec.get("REGION_WB"),
            "lat": float(rec["LABEL_Y"]) if rec.get("LABEL_Y") is not None else float(cen.iloc[i].y),
            "lon": float(rec["LABEL_X"]) if rec.get("LABEL_X") is not None else float(cen.iloc[i].x),
            "centroid_lat": float(cen.iloc[i].y), "centroid_lon": float(cen.iloc[i].x),
            "area_km2": float(area.iloc[i]), "pop_est": _ival(rec.get("POP_EST")),
            "gdp_md_usd": float(rec["GDP_MD"]) if rec.get("GDP_MD") not in (None, -99) else None,
            "gdp_year": _ival(rec.get("GDP_YEAR")),
            "neighbors_queen": sorted(q.neighbors.get(iso, [])), "neighbors_rook": sorted(r.neighbors.get(iso, [])),
            "sovereign_iso3": rec.get("SOV_A3"), "ne_version": version,
        })
    df = pl.DataFrame(rows).with_columns(pl.col("neighbors_queen").list.len().cast(pl.Int16).alias("n_neighbors"),
                                         pl.lit(now_utc()).alias("ingested_at"))
    return df


def haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray) -> np.ndarray:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi, dl = p2 - p1, np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def distance_matrix(geo: pl.DataFrame | None = None, settings: Settings | None = None) -> tuple[list[str], np.ndarray]:
    """(ids, n x n great-circle km between Natural Earth label points)."""
    if geo is None:
        from app.core.storage import read_table  # noqa: PLC0415

        geo = read_table("geo_countries", columns=["iso2", "lat", "lon"], settings=settings)
    geo = geo.sort("iso2")
    lat, lon = geo["lat"].to_numpy(), geo["lon"].to_numpy()
    d = haversine_km(lat[:, None], lon[:, None], lat[None, :], lon[None, :])
    np.fill_diagonal(d, 0.0)
    return geo["iso2"].to_list(), d


def run_geo(settings: Settings | None = None, force: bool = False) -> dict[str, Any]:
    import zipfile  # noqa: PLC0415

    s = settings_of(settings)
    z = download(s, force=force)
    version = None
    with zipfile.ZipFile(z) as zf:
        vf = [n for n in zf.namelist() if n.endswith("VERSION.txt")]
        if vf:
            version = zf.read(vf[0]).decode("utf-8", "ignore").strip()[:20]
    df = build_frame(read_countries(z), version)
    n = write(df, "geo_countries", s, mode="overwrite")
    rep = {"rows": n, "ne_version": version, "islands": int((df["n_neighbors"] == 0).sum())}
    log.info("linkage geo: %s", rep)
    update_progress("A1", "done", f"geo_countries {n} rows (NE {version}), queen/rook contiguity", s, **rep)
    return rep


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_geo(ctx.settings, force=str(p.get("force", "false")).lower() in ("1", "true"))
    return JobResult(status="success", rows_written={"geo_countries": rep["rows"]}, message=str(rep))
