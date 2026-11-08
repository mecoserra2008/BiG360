"""Unsupervised methods: K-means, hierarchical (agglomerative) clustering and PCA (owner: model-studio).

Feature matrix layouts:
  * rows  - every dataset row is an observation (cross-section: companies; time series: dates; panel: entity-dates)
  * pivot - panel only: entities are observations and the dates of one variable are the features
            (e.g. "cluster sectors by the correlation of their monthly returns")
"""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, clean, series

MAX_MEMBERS = 3000


class FeatureParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    standardize: bool = Field(default=True, description="z-score features before distances / PCA")
    layout: Literal["rows", "pivot"] = "rows"
    pivot_var: str | None = Field(default=None, description="pivot layout: the variable whose dates become features (default first x)")
    impute: Literal["drop", "median"] = Field(default="drop", description="rows with missing features: drop or fill with the median")
    max_missing_share: float = Field(default=0.2, ge=0, le=0.9, description="pivot: drop entities with more missing dates than this")


class KMeansParams(FeatureParams):
    k: int | None = Field(default=None, ge=2, le=50, description="fixed k; null = choose by silhouette over k range")
    k_min: int = Field(default=2, ge=2, le=50)
    k_max: int = Field(default=10, ge=2, le=50)
    n_init: int = Field(default=10, ge=1, le=100)
    max_iter: int = Field(default=300, ge=10, le=5000)


class HierParams(FeatureParams):
    linkage: Literal["ward", "average", "complete", "single"] = "ward"
    metric: Literal["euclidean", "correlation", "cosine"] = "euclidean"
    n_clusters: int | None = Field(default=None, ge=2, le=100, description="cut into k clusters")
    distance_threshold: float | None = Field(default=None, gt=0, description="or cut at this linkage distance")
    k_max: int = Field(default=10, ge=2, le=50, description="auto cut: best silhouette for k in 2..k_max")
    min_periods: int = Field(default=12, ge=3, le=1000, description="pivot + correlation: min overlapping periods")


class PCAParams(FeatureParams):
    n_components: int | None = Field(default=None, ge=1, le=100)


# ------------------------------------------------------------------------------------------------ features


def feature_matrix(data: FitData, p: FeatureParams, res: Result) -> tuple[np.ndarray, list[str], pd.DataFrame, np.ndarray]:
    """Returns (X standardized-or-raw, feature names, row info frame [label, entity_id?, date?, sector_id?], X_raw)."""
    if p.layout == "pivot":
        if not data.is_panel:
            raise GuardrailError("pivot_needs_panel", "layout=pivot needs a panel dataset")
        var = p.pivot_var or (data.x[0] if data.x else None)
        if var is None or var not in data.frame.columns:
            raise GuardrailError("pivot_var", "pivot layout needs pivot_var (a dataset column)")
        f = data.frame.select("entity_id", "entity_label", "date", var).to_pandas()
        wide = f.pivot_table(index=["entity_id", "entity_label"], columns="date", values=var, aggfunc="first")
        wide = wide.loc[:, wide.notna().mean(axis=0) >= 0.5]  # dates where most entities have data
        miss = wide.isna().mean(axis=1)
        dropped = wide.index[miss > p.max_missing_share]
        if len(dropped):
            res.info("entities_dropped", f"{len(dropped)} entities dropped (> {p.max_missing_share:.0%} missing dates)")
        wide = wide.loc[miss <= p.max_missing_share]
        raw = wide.to_numpy(dtype=float)
        col_mean = np.nanmean(raw, axis=0)
        raw = np.where(np.isnan(raw), col_mean, raw)
        info = pd.DataFrame({"entity_id": [i[0] for i in wide.index], "label": [str(i[1]) for i in wide.index]})
        names = [str(c)[:10] for c in wide.columns]
        res.summary.update({"layout": "pivot", "pivot_var": var, "n_features_dates": len(names)})
        return _scale(raw, p.standardize), names, info, raw
    cols = list(data.x)
    if not cols:
        raise GuardrailError("missing_x", "pick feature columns (x)")
    keep = ["date"] + (["entity_id", "entity_label"] if data.is_panel else []) + \
        [c for c in ("sector_id", "region", "country") if c in data.frame.columns]
    df = data.frame.select(list(dict.fromkeys(keep + cols))).to_pandas()
    for c in cols:
        df[c] = df[c].astype(float).replace([np.inf, -np.inf], np.nan)
    if p.impute == "median":
        n_imp = int(df[cols].isna().sum().sum())
        df[cols] = df[cols].fillna(df[cols].median())
        if n_imp:
            res.info("imputed", f"{n_imp} missing feature values filled with the column median")
        df = df.dropna(subset=cols)
    else:
        n0 = len(df)
        df = df.dropna(subset=cols)
        if n0 - len(df):
            res.summary["rows_dropped_missing"] = n0 - len(df)
    if data.is_panel:
        multi_date = df["date"].nunique() > 1
        label = (df["entity_label"].astype(str) + (" " + df["date"].astype(str) if multi_date else "")).tolist()
        info = pd.DataFrame({"entity_id": df["entity_id"].tolist(), "label": label})
    else:
        info = pd.DataFrame({"label": df["date"].astype(str).tolist()})
    info["date"] = df["date"].astype(str).tolist()
    for c in ("sector_id", "region", "country"):
        if c in df.columns:
            info[c] = df[c].tolist()
    raw = df[cols].to_numpy(dtype=float)
    res.summary.update({"layout": "rows", "features": cols})
    return _scale(raw, p.standardize), cols, info.reset_index(drop=True), raw


def _scale(X: np.ndarray, standardize: bool) -> np.ndarray:
    if not standardize:
        return X
    sd = X.std(axis=0, ddof=0)
    sd[sd == 0] = 1.0
    return (X - X.mean(axis=0)) / sd


def _check_n(n: int, need: int, what: str) -> None:
    if n < need:
        raise GuardrailError("too_few_obs", f"{what} needs at least {need} observations, got {n}")


def pca_scatter(res: Result, Xs: np.ndarray, info: pd.DataFrame, labels: np.ndarray | None, seed: int) -> None:
    from sklearn.decomposition import PCA

    if Xs.shape[1] < 2 or Xs.shape[0] < 3:
        return
    pc = PCA(n_components=2, random_state=seed).fit(Xs)
    sc = pc.transform(Xs)
    n = len(sc)
    idx = np.arange(n) if n <= 3000 else np.random.default_rng(seed).choice(n, 3000, replace=False)
    pts = [{"label": info["label"].iat[i], "cluster": int(labels[i]) if labels is not None else None,
            "x": float(sc[i, 0]), "y": float(sc[i, 1]),
            "sector_id": info["sector_id"].iat[i] if "sector_id" in info.columns else None} for i in idx]
    ev = pc.explained_variance_ratio_
    res.plots["pca_scatter"] = clean({"kind": "scatter_points", "title": "PCA projection (PC1 vs PC2)",
                                      "x_label": f"PC1 ({ev[0]:.0%})", "y_label": f"PC2 ({ev[1]:.0%})", "points": pts})


def cluster_outputs(res: Result, labels: np.ndarray, Xs: np.ndarray, raw: np.ndarray, names: list[str],
                    info: pd.DataFrame, centers_s: np.ndarray | None = None) -> None:
    ks = sorted(np.unique(labels).tolist())
    profiles, members = [], []
    overall = raw.mean(axis=0)
    for k in ks:
        m = labels == k
        prof: dict[str, Any] = {"cluster": int(k), "size": int(m.sum()),
                                "mean": dict(zip(names, raw[m].mean(axis=0).tolist(), strict=True)),
                                "median": dict(zip(names, np.median(raw[m], axis=0).tolist(), strict=True)),
                                "z_mean": dict(zip(names, Xs[m].mean(axis=0).tolist(), strict=True))}
        if "sector_id" in info.columns:
            vc = info.loc[m, "sector_id"].fillna("n/a").value_counts()
            prof["top_sectors"] = [{"sector_id": s, "count": int(c)} for s, c in vc.head(5).items()]
        profiles.append(prof)
        cen = centers_s[ks.index(k)] if centers_s is not None else Xs[m].mean(axis=0)
        dist = np.sqrt(((Xs[m] - cen) ** 2).sum(axis=1))
        sub = info.loc[m].copy()
        sub["distance"] = dist
        sub = sub.sort_values("distance")
        for _, r in sub.iterrows():
            members.append({"cluster": int(k), "label": r["label"], "entity_id": r.get("entity_id"),
                            "sector_id": r.get("sector_id"), "distance_to_center": float(r["distance"])})
    if len(members) > MAX_MEMBERS:
        res.info("members_truncated", f"member list truncated to {MAX_MEMBERS} rows")
        members = members[:MAX_MEMBERS]
    res.clusters = clean({"n_clusters": len(ks), "sizes": [p_["size"] for p_ in profiles], "profiles": profiles,
                          "members": members, "features": names, "overall_mean": dict(zip(names, overall.tolist(), strict=True))})
    rows = [[p_["cluster"], p_["size"]] + [p_["mean"][n] for n in names[:30]] for p_ in profiles]
    res.add_table("profiles", "Cluster profiles (feature means, original units)", ["cluster", "size"] + names[:30],
                  clean(rows))
    res.plots["profile_heatmap"] = clean({"kind": "heatmap", "title": "Cluster profiles (mean z-score)",
                                          "x": names[:60], "y": [f"cluster {k}" for k in ks],
                                          "z": [[p_["z_mean"][n] for n in names[:60]] for p_ in profiles]})


def _quality(res: Result, Xs: np.ndarray, labels: np.ndarray, metric: str = "euclidean", D: np.ndarray | None = None) -> None:
    from sklearn.metrics import calinski_harabasz_score, davies_bouldin_score, silhouette_score

    k = len(np.unique(labels))
    if 2 <= k < len(labels):
        sil = float(silhouette_score(D if D is not None else Xs, labels, metric="precomputed" if D is not None else metric))
        res.fit_stats["silhouette"] = sil
        res.metrics["silhouette"] = sil
        if D is None:
            res.fit_stats["calinski_harabasz"] = float(calinski_harabasz_score(Xs, labels))
            res.fit_stats["davies_bouldin"] = float(davies_bouldin_score(Xs, labels))
        if sil < 0.15:
            res.warn("weak_structure", f"silhouette {sil:.2f} < 0.15: little cluster structure; clusters may be arbitrary")
    smallest = int(np.bincount(labels.astype(int) - labels.min()).min())
    if smallest < 3:
        res.info("tiny_cluster", f"smallest cluster has {smallest} member(s)")


# ------------------------------------------------------------------------------------------------ methods


@register
class KMeansMethod(Method):
    id = "kmeans"
    family = "clustering"
    title = "K-means clustering"
    description = ("K-means over a k range: inertia (elbow) and silhouette per k, chosen k (best silhouette unless "
                   "fixed), centroids, member lists, cluster profiles and a PCA projection.")
    needs_y = False
    min_x = 1
    Params = KMeansParams
    outputs = ("elbow / silhouette curves", "centroids", "members", "profiles", "PCA scatter")
    diagnostics = ("silhouette", "Calinski-Harabasz", "Davies-Bouldin")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        ks = 1 if getattr(params, "k", None) else max(1, params.k_max - params.k_min + 1)
        return 0.2 + ks * params.n_init * n_rows * max(n_x, 1) * 4e-7 + ks * (n_rows ** 2) * 2e-8

    def fit(self, data: FitData, p: KMeansParams) -> Result:
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score

        res = Result(self.id, self.family, self.title)
        Xs, names, info, raw = feature_matrix(data, p, res)
        n = len(Xs)
        _check_n(n, 4, "k-means")
        k_lo, k_hi = (p.k, p.k) if p.k else (p.k_min, min(p.k_max, n - 1))
        rows, best = [], None
        sil_sample = min(n, 5000)
        for k in range(k_lo, k_hi + 1):
            data.check()
            km = KMeans(n_clusters=k, n_init=p.n_init, max_iter=p.max_iter, random_state=data.seed).fit(Xs)
            sil = float(silhouette_score(Xs, km.labels_, sample_size=sil_sample if n > 5000 else None,
                                         random_state=data.seed)) if 2 <= k < n else None
            rows.append([k, float(km.inertia_), sil])
            if best is None or (sil is not None and (best[1] is None or sil > best[1])):
                best = (k, sil, km)
        k, _, km = best if not p.k else (p.k, rows[0][2], best[2])
        res.add_table("k_selection", "k selection (inertia: elbow; silhouette: higher is better)",
                      ["k", "inertia", "silhouette"], clean(rows))
        res.line_plot("elbow", "Elbow (within-cluster sum of squares)", [series("inertia", [r[0] for r in rows], [r[1] for r in rows])], "k", "inertia")
        res.line_plot("silhouette", "Silhouette by k", [series("silhouette", [r[0] for r in rows], [r[2] for r in rows])], "k", "silhouette")
        labels = km.labels_
        _quality(res, Xs, labels)
        cen_s = km.cluster_centers_
        sd = raw.std(axis=0, ddof=0)
        sd[sd == 0] = 1
        cen_raw = cen_s * sd + raw.mean(axis=0) if p.standardize else cen_s
        res.add_table("centroids", "Centroids (original units)", ["cluster"] + names[:40],
                      clean([[i] + c[:40].tolist() for i, c in enumerate(cen_raw)]))
        cluster_outputs(res, labels, Xs, raw, names, info, cen_s)
        pca_scatter(res, Xs, info, labels, data.seed)
        res.summary.update({"n_obs": n, "k": int(k), "k_selected_by": "fixed" if p.k else "max silhouette",
                            "standardized": p.standardize})
        res.fit_stats.update({"inertia": float(km.inertia_), "k": int(k)})
        res.metrics.update({"k": int(k), "inertia": float(km.inertia_), "n_obs": n})
        if not p.k and len(rows) > 3:
            res.info("k_search", f"k chosen among {len(rows)} candidates by silhouette; check the elbow plot too")
        return res


@register
class Hierarchical(Method):
    id = "hierarchical"
    family = "clustering"
    title = "Hierarchical (agglomerative) clustering"
    description = ("scipy linkage (ward / average / complete / single) on euclidean, correlation or cosine "
                   "distances; dendrogram data; cut by k, by distance, or by best silhouette.")
    needs_y = False
    min_x = 1
    Params = HierParams
    outputs = ("dendrogram", "members", "profiles", "correlation heatmap (pivot)", "PCA scatter")
    diagnostics = ("cophenetic correlation", "silhouette")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.2 + (n_rows ** 2) * max(n_x, 1) * 5e-8

    def fit(self, data: FitData, p: HierParams) -> Result:
        from scipy.cluster.hierarchy import cophenet, dendrogram, fcluster, linkage
        from scipy.spatial.distance import pdist, squareform
        from sklearn.metrics import silhouette_score

        res = Result(self.id, self.family, self.title)
        if p.linkage == "ward" and p.metric != "euclidean":
            raise GuardrailError("ward_metric", "ward linkage requires the euclidean metric")
        Xs, names, info, raw = feature_matrix(data, p, res)
        n = len(Xs)
        _check_n(n, 3, "hierarchical clustering")
        if n > 5000:
            raise GuardrailError("too_many_obs", f"{n} observations: hierarchical clustering is O(n^2); filter to <= 5000")
        corr = None
        if p.metric == "correlation" and p.layout == "pivot":
            var = res.summary.get("pivot_var")
            f = data.frame.select("entity_id", "date", var).to_pandas().pivot_table(index="date", columns="entity_id", values=var)
            f = f[[e for e in info["entity_id"]]]
            corr = f.corr(min_periods=p.min_periods).to_numpy()
            if np.isnan(corr).any():
                res.warn("corr_nan", "some entity pairs have too few overlapping periods; their correlation was set to 0")
                corr = np.nan_to_num(corr, nan=0.0)
            np.fill_diagonal(corr, 1.0)
            D = np.clip(1.0 - corr, 0, 2)
            np.fill_diagonal(D, 0.0)
            D = (D + D.T) / 2
            dvec = squareform(D, checks=False)
        else:
            dvec = pdist(Xs, metric=p.metric)
            dvec = np.nan_to_num(dvec, nan=0.0)
            D = squareform(dvec)
        data.check()
        Z = linkage(dvec, method=p.linkage) if not (p.linkage == "ward" and corr is None) else linkage(Xs, method="ward")
        coph, _ = cophenet(Z, dvec)
        res.fit_stats["cophenetic_correlation"] = float(coph)
        if p.n_clusters:
            labels = fcluster(Z, t=min(p.n_clusters, n), criterion="maxclust")
            how = f"k = {p.n_clusters}"
        elif p.distance_threshold:
            labels = fcluster(Z, t=p.distance_threshold, criterion="distance")
            how = f"distance <= {p.distance_threshold}"
        else:
            rows, best = [], None
            for k in range(2, min(p.k_max, n - 1) + 1):
                lab = fcluster(Z, t=k, criterion="maxclust")
                if len(np.unique(lab)) < 2:
                    continue
                s = float(silhouette_score(D, lab, metric="precomputed"))
                rows.append([k, s])
                if best is None or s > best[1]:
                    best = (k, s, lab)
            if best is None:
                labels = np.ones(n, dtype=int)
                how = "single cluster"
            else:
                labels, how = best[2], f"k = {best[0]} (max silhouette)"
                res.add_table("k_selection", "Cut selection (silhouette on the distance matrix)", ["k", "silhouette"], clean(rows))
                res.line_plot("silhouette", "Silhouette by k", [series("silhouette", [r[0] for r in rows], [r[1] for r in rows])], "k", "silhouette")
        labels = labels.astype(int) - 1
        _quality(res, Xs, labels, D=D)
        lab_names = info["label"].astype(str).tolist()
        trunc = n > 200
        dd = dendrogram(Z, no_plot=True, labels=lab_names, truncate_mode="lastp" if trunc else None, p=60 if trunc else 30,
                        color_threshold=None)
        res.plots["dendrogram"] = clean({"kind": "dendrogram", "title": f"Dendrogram ({p.linkage}, {p.metric})",
                                         "icoord": dd["icoord"], "dcoord": dd["dcoord"], "leaf_labels": dd["ivl"],
                                         "leaves": dd["leaves"], "color_list": dd["color_list"], "truncated": trunc,
                                         "cut_height": float(Z[-(len(np.unique(labels))), 2]) if len(np.unique(labels)) > 1 else None})
        res.summary["linkage_matrix"] = clean(Z.tolist()) if n <= 500 else None
        if corr is not None and n <= 80:
            order = dd["leaves"] if not trunc else list(range(n))
            res.plots["correlation_heatmap"] = clean({"kind": "heatmap", "title": "Correlation matrix (dendrogram order)",
                                                      "x": [lab_names[i] for i in order], "y": [lab_names[i] for i in order],
                                                      "z": corr[np.ix_(order, order)]})
        cluster_outputs(res, labels, Xs, raw, names if p.layout == "rows" else names, info)
        pca_scatter(res, Xs, info, labels, data.seed)
        k = int(len(np.unique(labels)))
        res.summary.update({"n_obs": n, "k": k, "cut": how, "linkage": p.linkage, "metric": p.metric})
        res.metrics.update({"k": k, "cophenetic_correlation": float(coph), "n_obs": n})
        return res


@register
class PCAMethod(Method):
    id = "pca"
    family = "clustering"
    title = "Principal component analysis"
    description = "PCA on (standardized) features: explained variance, loadings, 2-D scores and component time series."
    needs_y = False
    min_x = 2
    Params = PCAParams
    outputs = ("scree / explained variance", "loadings", "scores scatter", "score time series (time-series data)")
    diagnostics = ("components for 80% / 90% variance",)

    def fit(self, data: FitData, p: PCAParams) -> Result:
        from sklearn.decomposition import PCA

        res = Result(self.id, self.family, self.title)
        Xs, names, info, raw = feature_matrix(data, p, res)
        n, m = Xs.shape
        _check_n(n, 3, "PCA")
        if m < 2:
            raise GuardrailError("too_few_features", "PCA needs at least 2 features")
        nc = min(p.n_components or min(10, m), m, n)
        pc = PCA(n_components=nc, random_state=data.seed).fit(Xs)
        sc = pc.transform(Xs)
        ev, cum = pc.explained_variance_ratio_, np.cumsum(pc.explained_variance_ratio_)
        comps = [f"PC{i + 1}" for i in range(nc)]
        res.add_table("explained_variance", "Explained variance", ["component", "eigenvalue", "share", "cumulative"],
                      clean([[c, pc.explained_variance_[i], ev[i], cum[i]] for i, c in enumerate(comps)]))
        res.line_plot("scree", "Scree plot", [series("share", comps, ev), series("cumulative", comps, cum)], "component",
                      "variance share", kind="bar")
        load = pc.components_.T  # features x comps
        res.add_table("loadings", "Loadings (feature weights per component)", ["feature"] + comps[:10],
                      clean([[names[j]] + load[j, :10].tolist() for j in range(m)]))
        res.plots["loadings_heatmap"] = clean({"kind": "heatmap", "title": "Loadings", "x": comps[:10],
                                               "y": names[:200], "z": load[:200, :10]})
        if nc >= 2:
            res.plots["biplot"] = clean({"kind": "arrows", "title": "Loadings PC1 vs PC2",
                                         "arrows": [{"feature": names[j], "x": float(load[j, 0]), "y": float(load[j, 1])}
                                                    for j in range(min(m, 200))]})
            pca_scatter_from(res, sc, info, ev)
        if not data.is_panel and p.layout == "rows":
            dates = info["date"].tolist()
            res.line_plot("scores_time", "Component scores over time",
                          [series(c, dates, sc[:, i]) for i, c in enumerate(comps[:3])], "date", "score")
        k80 = int(np.searchsorted(cum, 0.8) + 1) if cum[-1] >= 0.8 else None
        k90 = int(np.searchsorted(cum, 0.9) + 1) if cum[-1] >= 0.9 else None
        res.summary.update({"n_obs": n, "n_features": m, "n_components": nc, "components_for_80pct": k80,
                            "components_for_90pct": k90, "standardized": p.standardize})
        res.fit_stats.update({"pc1_share": float(ev[0]), "cumulative_share": float(cum[-1])})
        res.metrics.update({"pc1_share": float(ev[0]), "pc3_cumulative": float(cum[min(2, nc - 1)]), "n_obs": n})
        if not p.standardize:
            res.info("unstandardized", "features not standardized: components are dominated by high-variance features")
        return res


def pca_scatter_from(res: Result, sc: np.ndarray, info: pd.DataFrame, ev: np.ndarray) -> None:
    n = len(sc)
    idx = np.arange(n) if n <= 3000 else np.linspace(0, n - 1, 3000).astype(int)
    pts = [{"label": info["label"].iat[i], "cluster": None, "x": float(sc[i, 0]), "y": float(sc[i, 1]),
            "sector_id": info["sector_id"].iat[i] if "sector_id" in info.columns else None} for i in idx]
    res.plots["pca_scatter"] = clean({"kind": "scatter_points", "title": "Scores PC1 vs PC2",
                                      "x_label": f"PC1 ({ev[0]:.0%})", "y_label": f"PC2 ({ev[1]:.0%})", "points": pts})
