"""Spatial weights for the event models (owner: event-models).

`get_weights(kind, codes, as_of)` returns a dense N x N matrix (zero diagonal, NOT row-standardized) over the ISO2
`codes` that it could place, preferring the linkage agent's `app.linkage.weights.get_W(kind, level, as_of)` (table
`spatial_weights`, plan A7) and falling back to our own geography from Natural Earth admin-0 (public domain, 1:50m,
cached under data/raw/event_models/):

* contiguity:        queen contiguity of the country polygons; units without a land neighbour (islands) get their
                     nearest neighbour so no row is empty
* inverse_distance:  1 / great-circle distance (km) between the centroids of each country's largest polygon,
                     with a 4000 km soft cutoff (weights beyond it decay as 1/d^2)
* knn:               k nearest centroids (k = 5 default), symmetrized
Linkage-only kinds (trade, io, banking, comention, ...) raise WeightsUnavailable until the linkage tables exist.
"""

from __future__ import annotations

import functools
import io
import math
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

NE_URL = "https://naciscdn.org/naturalearth/50m/cultural/ne_50m_admin_0_countries.zip"
NE_FILE = "ne_50m_admin_0_countries.zip"
FALLBACK_KINDS = ("contiguity", "inverse_distance", "knn")
LINKAGE_KINDS = ("trade", "io", "banking", "comention", "distance", "contiguity", "knn", "inverse_distance")
# aliases accepted from callers / linkage naming
_ALIAS = {"queen": "contiguity", "rook": "contiguity", "distance": "inverse_distance", "idw": "inverse_distance",
          "geo": "inverse_distance", "geographic": "inverse_distance"}
EA_PROXY = "DE"   # EA aggregate placed at Germany's centroid when a dataset uses EA as a unit


class WeightsUnavailable(RuntimeError):
    pass


def _ne_path(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    d = s.data_dir / "raw" / "event_models"
    d.mkdir(parents=True, exist_ok=True)
    return d / NE_FILE


def _download_ne(path: Path) -> None:
    import urllib.request  # noqa: PLC0415

    log.info("weights: downloading Natural Earth admin-0 (%s)", NE_URL)
    with urllib.request.urlopen(urllib.request.Request(NE_URL, headers={"User-Agent": "Mozilla/5.0"}), timeout=120) as f:
        content = f.read()
    zipfile.ZipFile(io.BytesIO(content)).testzip()
    path.write_bytes(content)


@functools.lru_cache(maxsize=1)
def geography(data_dir: str | None = None) -> dict[str, Any]:
    """{codes, lat, lon, neighbours: {iso2: set}} from Natural Earth (largest-polygon centroids, queen contiguity)."""
    import geopandas as gpd  # noqa: PLC0415
    from shapely.geometry import MultiPolygon  # noqa: PLC0415

    s = get_settings()
    path = Path(data_dir) / "raw" / "event_models" / NE_FILE if data_dir else _ne_path(s)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _download_ne(path)
    g = gpd.read_file(f"zip://{path.as_posix()}")
    iso = g["ISO_A2"].where(g["ISO_A2"].str.fullmatch("[A-Z]{2}"), g["ISO_A2_EH"])   # France / Norway -99, Taiwan CN-TW
    g = g.assign(iso2=iso)
    g = g[(g["iso2"].str.len() == 2) & (g["iso2"] != "-9")].copy()
    g = g.dissolve(by="iso2", as_index=False)

    def main_poly(geom):
        if isinstance(geom, MultiPolygon):
            return max(geom.geoms, key=lambda p: p.area)
        return geom

    gm = g.set_geometry(g.geometry.apply(main_poly), crs=g.crs)
    cent = gm.to_crs("+proj=eqearth").centroid.to_crs(4326)
    codes = g["iso2"].tolist()
    lat, lon = cent.y.to_numpy(), cent.x.to_numpy()
    nb: dict[str, set[str]] = {c: set() for c in codes}
    sidx = g.sindex
    geoms = g.geometry.buffer(0).tolist()
    for i, gi in enumerate(geoms):
        for j in sidx.query(gi, predicate="intersects"):
            if j != i:
                nb[codes[i]].add(codes[int(j)])
    return {"codes": codes, "lat": dict(zip(codes, lat.tolist())), "lon": dict(zip(codes, lon.tolist())), "neighbours": nb}


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(a)))


def distance_matrix(codes: list[str], geo: dict[str, Any] | None = None) -> np.ndarray:
    geo = geo or geography()
    n = len(codes)
    D = np.zeros((n, n))
    cc = [EA_PROXY if c == "EA" else c for c in codes]
    for i in range(n):
        for j in range(i + 1, n):
            D[i, j] = D[j, i] = max(haversine_km(geo["lat"][cc[i]], geo["lon"][cc[i]], geo["lat"][cc[j]], geo["lon"][cc[j]]), 50.0)
    return D


def _fallback(kind: str, codes: list[str], k: int = 5, cutoff_km: float = 4000.0) -> tuple[list[str], np.ndarray]:
    geo = geography()
    have = [c for c in codes if (EA_PROXY if c == "EA" else c) in geo["lat"]]
    n = len(have)
    if n < 2:
        raise WeightsUnavailable(f"fewer than 2 placeable units among {codes[:10]}")
    D = distance_matrix(have, geo)
    W = np.zeros((n, n))
    if kind == "inverse_distance":
        W = np.where(D > 0, 1.0 / D, 0.0)
        far = D > cutoff_km
        W[far] = (cutoff_km / D[far]) / D[far]   # continuous at the cutoff, then 1/d^2
    elif kind == "knn":
        kk = min(k, n - 1)
        for i in range(n):
            order = np.argsort(D[i] + np.where(np.arange(n) == i, np.inf, 0))
            W[i, order[:kk]] = 1.0
        W = np.maximum(W, W.T)
    elif kind == "contiguity":
        idx = {c: i for i, c in enumerate(have)}
        for c in have:
            for d in geo["neighbours"].get(EA_PROXY if c == "EA" else c, ()):
                if d in idx:
                    W[idx[c], idx[d]] = W[idx[d], idx[c]] = 1.0
        for i in range(n):  # islands / no neighbour in the set -> nearest unit
            if W[i].sum() == 0:
                j = int(np.argmin(D[i] + np.where(np.arange(n) == i, np.inf, 0)))
                W[i, j] = W[j, i] = 1.0
    else:
        raise WeightsUnavailable(f"kind {kind!r} needs the linkage spatial_weights (not built yet)")
    np.fill_diagonal(W, 0.0)
    return have, W


#: our kind names -> linkage spatial_weights kinds (level country)
LINKAGE_KIND = {"contiguity": "contiguity_queen", "rook": "contiguity_rook", "inverse_distance": "inv_distance",
                "knn": "knn5", "trade": "trade", "trade_exports": "trade_exports", "io": "io_upstream",
                "io_upstream": "io_upstream", "banking": "bank_claims", "bank_claims": "bank_claims"}


@functools.lru_cache(maxsize=32)
def _linkage_full(kind: str, as_of: date | None) -> tuple[tuple[str, ...], np.ndarray] | None:
    try:
        from app.linkage.weights import get_W  # type: ignore  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - linkage not landed yet
        return None
    try:
        nodes, m = get_W(LINKAGE_KIND.get(kind, kind), "country", as_of, standardize="raw", fmt="sparse")
    except Exception as e:  # noqa: BLE001
        log.info("weights: linkage get_W(%s) unavailable: %s -> fallback", kind, e)
        return None
    return tuple(nodes), np.asarray(m.todense(), dtype=float)


def _from_linkage(kind: str, codes: list[str], as_of: date | None) -> tuple[list[str], np.ndarray, dict[str, Any]] | None:
    got = _linkage_full(kind, as_of)
    if got is None:
        return None
    labels, M = got
    pos = {c: i for i, c in enumerate(labels)}
    contig = kind in ("contiguity", "rook")
    if contig:   # islands have no stored cells: keep every placeable country, filled with its nearest neighbour below
        try:
            geo = geography()["lat"]
        except Exception:  # noqa: BLE001
            geo = {}
        have = [c for c in codes if c in pos or (EA_PROXY if c == "EA" else c) in geo]
    else:
        have = [c for c in codes if c in pos]
    if len(have) < 2:
        return None
    W = np.zeros((len(have), len(have)))
    inn = [k for k, c in enumerate(have) if c in pos]
    ix = [pos[have[k]] for k in inn]
    W[np.ix_(inn, inn)] = M[np.ix_(ix, ix)]
    np.fill_diagonal(W, 0.0)
    if contig:   # islands in the subset -> nearest neighbour (as in the fallback); units without a centroid kept as is
        try:
            geo = geography()
            ok = [k for k, c in enumerate(have) if (EA_PROXY if c == "EA" else c) in geo["lat"]]
            D = distance_matrix([have[k] for k in ok], geo)
            for a, i in enumerate(ok):
                if W[i].sum() == 0 and len(ok) > 1:
                    b = int(np.argmin(D[a] + np.where(np.arange(len(ok)) == a, np.inf, 0)))
                    W[i, ok[b]] = W[ok[b], i] = 1.0
        except Exception as e:  # noqa: BLE001
            log.warning("weights: island fill failed: %s", e)
    return have, W, {"source": "linkage", "kind": kind, "linkage_kind": LINKAGE_KIND.get(kind, kind)}


def _coerce(obj: Any) -> tuple[list[str] | None, np.ndarray | None]:
    """Accept (labels, matrix), a libpysal W, a pandas square DataFrame or a long frame (src, dst, w)."""
    try:
        if isinstance(obj, tuple) and len(obj) >= 2:
            return list(obj[0]), np.asarray(obj[1], dtype=float)
        if hasattr(obj, "full") and hasattr(obj, "id_order"):
            M, ids = obj.full()
            return list(ids), M
        import pandas as pd  # noqa: PLC0415
        import polars as pl  # noqa: PLC0415

        if isinstance(obj, pl.DataFrame):
            obj = obj.to_pandas()
        if isinstance(obj, pd.DataFrame):
            if obj.shape[0] == obj.shape[1] and list(obj.index) == list(obj.columns):
                return list(obj.index), obj.to_numpy(float)
            cols = [c.lower() for c in obj.columns]
            src = next(c for c in obj.columns if c.lower() in ("src", "from", "origin", "i", "src_id", "from_id"))
            dst = next(c for c in obj.columns if c.lower() in ("dst", "to", "dest", "j", "dst_id", "to_id"))
            wc = next(c for c in obj.columns if c.lower() in ("w", "weight", "value", "w_raw", "raw"))
            del cols
            labels = sorted(set(obj[src]) | set(obj[dst]))
            pos = {c: i for i, c in enumerate(labels)}
            M = np.zeros((len(labels), len(labels)))
            for a, b, v in obj[[src, dst, wc]].itertuples(index=False):
                M[pos[a], pos[b]] = float(v)
            return labels, M
    except Exception as e:  # noqa: BLE001
        log.info("weights: could not read the linkage weights object (%s)", e)
    return None, None


def linkage_available() -> bool:
    try:
        from app.linkage.weights import get_W  # type: ignore  # noqa: F401, PLC0415
        return True
    except Exception:  # noqa: BLE001
        return False


def get_weights(kind: str, codes: list[str], as_of: date | None = None, k: int = 5,
                prefer_linkage: bool = True) -> tuple[list[str], np.ndarray, dict[str, Any]]:
    """-> (codes placed, raw W (zero diagonal), meta {source, kind, n})."""
    kind = _ALIAS.get(kind, kind)
    codes = list(dict.fromkeys(codes))
    if prefer_linkage:
        got = _from_linkage(kind, codes, as_of)
        if got is not None:
            have, W, meta = got
            return have, W, meta | {"n": len(have)}
    have, W = _fallback(kind, codes, k=k)
    return have, W, {"source": "natural_earth_fallback", "kind": kind, "n": len(have), "k": k if kind == "knn" else None}
