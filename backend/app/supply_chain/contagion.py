"""MODEL-BASED contagion propagation over the disclosed supply-chain network + portfolio exposure aggregation.

Transparent damped propagation (docs/SUPPLY_CHAIN.md §contagion):
  nodes   issuers (and unresolved counterparties) of sc_edges; a shock is a fractional REVENUE change (-0.10 = -10%)
  demand channel (upstream)    a revenue change x_c of customer c reaches supplier j as  s_rev(j, c) x x_c
                               (s_rev = share of j's revenue from c; c's purchases are assumed to move with its revenue)
  supply channel (downstream)  an output change x_j of supplier j reaches customer c as  gamma x s_pur(j, c) x x_j
                               (s_pur = share of c's purchases from j; gamma = supply_factor, default 0.5: disruptions
                               are partly substituted / buffered by inventory)
  missing shares               default_share (0.05) - flagged "assumed" on the edge
  edge confidence              multiplies the transmission (expected value over the edge's existence)
  M = demand + supply matrix (M[i, j] = transmission from j to i); hop h impact  x(h) = delta^(h-1) M^h x(0)  for h >= 1
  (delta = damping, default 0.5: second- and later-round effects are damped); total = x(0) + sum_{h=1..H} x(h), H <= 3;
  each node's total is clipped to [-1, 1] x max |seed| (no amplification beyond the original shock).
  Country shock of size m on country K: x(0)_i = m x w_i(K) (direct revenue share of i in K, sc_exposure), then the
  same network rounds. Statistical neighbours (optional): one hop, rho x delta x x_seed, reported separately (co-movement,
  not causal). The macro channel (W4 factor betas) is added by the API as a separate equity-return estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

MODEL_LABEL = ("Model-based estimate: damped propagation of a revenue shock over disclosed supply-chain links "
               "(not a forecast)")
DEFAULTS = {"damping": 0.5, "hops": 3, "supply_factor": 0.5, "default_share": 0.05}


@dataclass
class Edge:
    supplier: str
    customer: str
    s_rev: float | None = None
    s_pur: float | None = None
    confidence: float = 1.0


@dataclass
class Result:
    impact: dict[str, float] = field(default_factory=dict)          # total (incl. seeds)
    by_hop: dict[str, list[float]] = field(default_factory=dict)    # per node [x0, x1, ..., xH]
    first_hop: dict[str, int] = field(default_factory=dict)         # hop at which the node is first reached
    pred: dict[str, str | None] = field(default_factory=dict)       # strongest predecessor at the first-reach hop
    channel: dict[str, str] = field(default_factory=dict)           # demand | supply | seed
    assumed: set[tuple[str, str]] = field(default_factory=set)      # edges that used default_share


def transmission(edges: list[Edge], nodes: list[str], demand: bool = True, supply: bool = True,
                 supply_factor: float = DEFAULTS["supply_factor"], default_share: float = DEFAULTS["default_share"]
                 ) -> tuple[Any, Any, set[tuple[str, str]]]:
    """Sparse M (demand + supply) and the demand-only part (for channel labels): M[i, j] = transmission j -> i."""
    from scipy import sparse  # noqa: PLC0415

    ix = {n: k for k, n in enumerate(nodes)}
    r, c, v, rd, cd, vd = [], [], [], [], [], []
    assumed: set[tuple[str, str]] = set()
    for e in edges:
        if e.supplier not in ix or e.customer not in ix or e.supplier == e.customer:
            continue
        w = max(0.0, min(1.0, float(e.confidence if e.confidence is not None else 1.0)))
        if demand:
            s = e.s_rev
            if s is None:
                s = default_share
                assumed.add((e.supplier, e.customer))
            r.append(ix[e.supplier]); c.append(ix[e.customer]); v.append(w * min(1.0, s))  # noqa: E702
            rd.append(ix[e.supplier]); cd.append(ix[e.customer]); vd.append(w * min(1.0, s))  # noqa: E702
        if supply:
            s = e.s_pur
            if s is None:
                s = default_share
                assumed.add((e.supplier, e.customer))
            r.append(ix[e.customer]); c.append(ix[e.supplier]); v.append(w * supply_factor * min(1.0, s))  # noqa: E702
    n = len(nodes)
    M = sparse.csr_matrix((v, (r, c)), shape=(n, n))
    Md = sparse.csr_matrix((vd, (rd, cd)), shape=(n, n))
    return M, Md, assumed


def propagate(edges: list[Edge], seeds: dict[str, float], damping: float = DEFAULTS["damping"],
              hops: int = DEFAULTS["hops"], demand: bool = True, supply: bool = True,
              supply_factor: float = DEFAULTS["supply_factor"], default_share: float = DEFAULTS["default_share"]) -> Result:
    nodes = sorted({e.supplier for e in edges} | {e.customer for e in edges} | set(seeds))
    ix = {n: k for k, n in enumerate(nodes)}
    hops = max(0, min(int(hops), 6))
    M, Md, assumed = transmission(edges, nodes, demand, supply, supply_factor, default_share)
    x0 = np.zeros(len(nodes))
    for k, val in seeds.items():
        x0[ix[k]] = float(val)
    res = Result(assumed=assumed)
    cap = float(np.max(np.abs(x0))) if len(x0) else 0.0
    layers = [x0]
    first = {nodes[i]: 0 for i in np.flatnonzero(x0)}
    pred: dict[str, str | None] = dict.fromkeys(first)
    chan = dict.fromkeys(first, "seed")
    y = x0                                                          # y(h) = M^h x0 (undamped)
    for h in range(1, hops + 1):
        scale = damping ** (h - 1)                                  # x(h) = delta^(h-1) y(h)
        contrib = M.multiply(y[np.newaxis, :]).tocsr()              # [i, j] = M[i, j] x y_j
        y_new = np.asarray(contrib.sum(axis=1)).ravel()
        dem = np.asarray(Md.multiply(y[np.newaxis, :]).sum(axis=1)).ravel()
        for i in np.flatnonzero(np.abs(y_new) > 1e-12):
            nd = nodes[i]
            if nd in first:
                continue
            row = contrib.getrow(i)
            j = row.indices[np.argmax(np.abs(row.data))] if row.nnz else None
            first[nd] = h
            pred[nd] = nodes[j] if j is not None else None
            chan[nd] = "demand" if abs(dem[i]) >= abs(y_new[i]) / 2 else "supply"
        layers.append(scale * y_new)
        y = y_new
    tot = np.sum(layers, axis=0)
    if cap > 0:
        tot = np.clip(tot, -cap, cap)
    for k, nd in enumerate(nodes):
        if nd in first:
            res.impact[nd] = float(tot[k])
            res.by_hop[nd] = [float(layer[k]) for layer in layers]
    res.first_hop, res.pred, res.channel = first, pred, chan
    return res


def path_to(res: Result, node: str, limit: int = 6) -> list[str]:
    """Seed -> ... -> node along the strongest first-reach predecessors."""
    out = [node]
    cur = node
    for _ in range(limit):
        p = res.pred.get(cur)
        if p is None or p in out:
            break
        out.append(p)
        cur = p
    return out[::-1]


def country_seeds(exposure: dict[str, dict[str, float]], iso2: str, magnitude: float) -> dict[str, float]:
    """issuer -> magnitude x direct revenue share in the shocked country."""
    return {iid: magnitude * w[iso2] for iid, w in exposure.items() if w.get(iso2)}


def statistical_channel(neighbours: list[tuple[str, str, float]], seeds: dict[str, float],
                        damping: float = DEFAULTS["damping"]) -> dict[str, float]:
    """One hop over (issuer, neighbour, rho) pairs: rho x damping x seed (co-movement proxy, not causal)."""
    out: dict[str, float] = {}
    for a, b, rho in neighbours:
        if a in seeds and b not in seeds:
            v = rho * damping * seeds[a]
            if abs(v) > abs(out.get(b, 0.0)):
                out[b] = v
    return out


# ================================================================================================ aggregation
def aggregate_exposure(weights: dict[str, float], exposure: dict[str, dict[str, dict[str, float]]],
                       field_name: str = "direct") -> dict[str, float]:
    """Portfolio country (or sector) exposure: sum_i w_i x exposure_i[k] over names with data (weights re-normalised)."""
    have = {i: w for i, w in weights.items() if exposure.get(i, {}).get(field_name)}
    tot = sum(have.values())
    if tot <= 0:
        return {}
    out: dict[str, float] = {}
    for i, w in have.items():
        for k, v in exposure[i][field_name].items():
            out[k] = out.get(k, 0.0) + w / tot * v
    return out
