"""Spatial weight construction properties (A7): symmetry where expected, row sums = 1, get_W API."""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

GEO = [  # 4 toy countries on a line + one island
    ("AA", 0.0, 0.0, ["BB"]), ("BB", 0.0, 10.0, ["AA", "CC"]), ("CC", 0.0, 20.0, ["BB", "DD"]),
    ("DD", 0.0, 30.0, ["CC"]), ("II", 40.0, 0.0, []),
]


def _geo_frame() -> pl.DataFrame:
    from app.core.schema import conform

    rows = [{"iso2": a, "lat": la, "lon": lo, "neighbors_queen": nb, "neighbors_rook": nb, "n_neighbors": len(nb),
             "gdp_md_usd": 100.0 * (k + 1), "ingested_at": pl.datetime(2026, 1, 1, time_zone="UTC")}
            for k, (a, la, lo, nb) in enumerate(GEO)]
    df = pl.DataFrame([{k: v for k, v in r.items() if k != "ingested_at"} for r in rows]).with_columns(
        pl.lit(None, pl.Datetime("us", "UTC")).fill_null(pl.datetime(2026, 1, 1, time_zone="UTC")).alias("ingested_at"))
    return conform(df, "geo_countries")


def _row_sums(df: pl.DataFrame) -> pl.Series:
    return df.group_by("i").agg(pl.col("w_row").sum())["w_row"]


def test_frame_row_standardization_and_no_diagonal():
    from app.linkage.weights import _frame

    f = _frame("x", "country", "2020", date(2021, 6, 30), ["A", "A", "A", "B", "B"], ["B", "C", "A", "A", "C"],
               [1.0, 3.0, 5.0, 2.0, 0.0], "v1")
    assert f.filter(pl.col("i") == pl.col("j")).is_empty()  # diagonal dropped
    assert f.filter(pl.col("w_raw") <= 0).is_empty()  # zeros dropped
    assert np.allclose(_row_sums(f).to_numpy(), 1.0)
    assert f.filter((pl.col("i") == "A") & (pl.col("j") == "C"))["w_row"].item() == pytest.approx(0.75)


def test_geo_weights_properties(tmp_data):
    from app.linkage.common import write
    from app.linkage.weights import build_geo, get_W

    write(_geo_frame(), "geo_countries", tmp_data, mode="overwrite")
    frames = {f["kind"][0]: f for f in build_geo(tmp_data, "v1")}
    for k in ("contiguity_queen", "contiguity_rook", "inv_distance"):  # symmetric raw weights
        f = frames[k]
        m = {(a, b): w for a, b, w in f.select("i", "j", "w_raw").iter_rows()}
        assert all(m.get((b, a)) == pytest.approx(w) for (a, b), w in m.items()), k
        assert np.allclose(_row_sums(f).to_numpy(), 1.0), k
    knn = frames["knn5"]
    assert (knn.group_by("i").len()["len"] == 4).all()  # n-1 neighbours when n <= k
    # nearest of AA is BB: inverse distance weight AA->BB > AA->DD
    inv = frames["inv_distance"]
    w = dict(inv.filter(pl.col("i") == "AA").select("j", "w_raw").iter_rows())
    assert w["BB"] > w["CC"] > w["DD"]
    from app.core.storage import write_table

    write_table(pl.concat(list(frames.values())).with_columns(
        pl.lit(None, pl.Datetime("us", "UTC")).fill_null(pl.datetime(2026, 1, 1, time_zone="UTC")).alias("built_at")),
        "spatial_weights", mode="overwrite", settings=tmp_data)
    W = get_W("contiguity_queen", settings=tmp_data)
    assert W.n == 5 and W.islands == ["II"]  # all geo_countries, island kept as an isolate
    rs = np.asarray(W.sparse.sum(axis=1)).ravel()
    assert np.allclose(rs[:4], 1.0) and rs[4] == 0
    Wb = get_W("contiguity_queen", standardize="b", settings=tmp_data)
    assert (Wb.sparse != Wb.sparse.T).nnz == 0  # binary queen is symmetric
    ids, S = get_W("contiguity_queen", fmt="sparse", ids=["AA", "BB", "CC", "DD", "II"], settings=tmp_data)
    assert S.shape == (5, 5) and S[4].nnz == 0  # island kept as an empty row
    sub = get_W("inv_distance", ids=["AA", "BB", "CC"], settings=tmp_data)  # re-standardized over the subset
    assert np.allclose(np.asarray(sub.sparse.sum(axis=1)).ravel(), 1.0)


def test_trade_matrix_total_is_symmetric():
    from app.linkage.weights import trade_matrix

    t = pl.DataFrame({
        "source": ["imf_imts"] * 5 + ["comtrade"],
        "reporter_iso2": ["US", "US", "CN", "CN", "DE", "US"], "partner_iso2": ["CN", "CN", "US", "US", "US", "CN"],
        "year": [2020] * 6, "flow": ["X", "M", "X", "M", "X", "X"], "cmd": ["TOTAL"] * 6,
        "value_usd": [100.0, 400.0, 380.0, 110.0, 50.0, 999.0]})
    tot = trade_matrix(t)
    m = {(a, b): v for a, b, v in tot.select("i", "j", "total").iter_rows()}
    assert m[("US", "CN")] == pytest.approx(m[("CN", "US")])
    assert m[("US", "CN")] == pytest.approx(100.0 + 380.0)  # IMF preferred over Comtrade; reporters' own exports
    assert m[("US", "DE")] == pytest.approx(50.0)  # DE exports only: mirror-filled for the US row


def test_allocate_exposure_sums_to_one_and_spreads_regions():
    from app.linkage.weights import allocate_exposure

    rows = pl.DataFrame({"member_type": ["country", "region", "rest_of_world", "other"],
                         "iso2": ["US", None, None, None], "region": [None, "NORDICS", None, None],
                         "value": [50.0, 30.0, 20.0, 999.0]})
    gdp = {"US": 20.0, "SE": 1.0, "NO": 1.0, "DK": 2.0, "FI": 0.0, "JP": 5.0}
    sh = allocate_exposure(rows, gdp)
    assert sum(sh.values()) == pytest.approx(1.0)
    assert sh["US"] == pytest.approx(0.5)  # US not re-spread from the rest-of-world pool
    assert sh["DK"] > sh["SE"]  # region spread by GDP
    assert "FI" not in sh  # zero GDP gets nothing
    assert sh["JP"] == pytest.approx(0.2 * 5 / (1 + 1 + 2 + 5))


def test_same_version_rebuild_drops_stale_cells(tmp_data):
    from app.linkage.weights import _frame, get_W, replace_version

    def frame(pairs):
        f = _frame("trade", "country", "2020", date(2021, 6, 30), [a for a, _ in pairs], [b for _, b in pairs],
                   [1.0] * len(pairs), "v1")
        return f.with_columns(pl.lit(None, pl.Datetime("us", "UTC")).fill_null(
            pl.datetime(2026, 1, 1, time_zone="UTC")).alias("built_at"))

    replace_version(frame([("AA", "BB"), ("AA", "CC"), ("BB", "AA")]), "v1", tmp_data)
    replace_version(frame([("AA", "BB"), ("BB", "AA")]), "v1", tmp_data)  # AA->CC disappeared
    W = get_W("trade", as_of="2022-01-01", standardize="raw", settings=tmp_data)
    assert dict(zip(W.neighbors["AA"], W.weights["AA"])) == {"BB": 1.0}
    with pytest.raises(KeyError):
        get_W("trade", as_of="2021-01-01", settings=tmp_data)  # not yet known (PIT)
