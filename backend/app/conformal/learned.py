"""Learned filters (torch CPU): the GCN-GRU graph filter of the paper (GNF, App. C.2) and a small horizon GRU head
for the per-security price envelope (application A).

GCN-GRU (paper eq. 34-40): per-node hidden state h_i in R^d (d = 32), node-shared weights, graph shift
S = D~^-1/2 (A + I) D~^-1/2 (fixed), gates z, r and candidate n each see [h_i, (S H)_i, y_i]; node-shared heads give
the mean, d_i = max(exp(w_d' h_i + b_d), 1e-4) and the row L_i in R^r (r = 4); Sigma = diag(d) + L L'. Training:
Gaussian NLL (Woodbury / matrix-determinant lemma), Adam (1e-3, betas 0.9/0.999, weight decay 1e-4), gradient
clipping 5.0, teacher forcing, truncated BPTT window 64, 10% loss burn-in. Glorot init for input-facing blocks,
orthogonal for state-facing blocks, zero biases, H_0 = 0.

Documented deviations (finance, not traffic):
  * inputs are standardised per node by the PRETRAIN standard deviation s_i (frozen) and the emitted law is mapped
    back (Sigma = diag(s) Sigma_z diag(s)); per-node input features are [y_i / s_i, |y_i| / s_i] (f_in = 2) so the
    recurrence sees volatility clustering directly (paper: f_in = 1 raw sensor value);
  * one model is trained jointly over all peer groups (weights are node-shared, so the parameter count does not
    depend on N; groups are padded to a common N with masked nodes) - more data per refit, one training per date;
  * epochs default 30 (paper 60; validation NLL plateaued earlier in our pilot), configurable.

Threads are capped at 4 (RAM / CPU budget of the live app).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.conformal.filters import Filter
from app.conformal.scores import EPS_D, Cov

TORCH_THREADS = 4


def _torch():
    import torch  # noqa: PLC0415

    torch.set_num_threads(TORCH_THREADS)
    return torch


def graph_shift(A: np.ndarray) -> np.ndarray:
    """S = D~^-1/2 (A + I) D~^-1/2 with symmetric non-negative A (zero diagonal ignored)."""
    A = np.asarray(A, dtype=np.float64)
    A = np.maximum(A, A.T)
    np.fill_diagonal(A, 0.0)
    At = A + np.eye(A.shape[0])
    dg = At.sum(1)
    di = 1.0 / np.sqrt(dg)
    return At * di[:, None] * di[None, :]


# ------------------------------------------------------------------------------------------------ modules


def _build_modules(d: int, fin: int, r: int):  # noqa: ANN202
    torch = _torch()
    nn = torch.nn

    class Cell(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.Wz = nn.Linear(2 * d + fin, d)
            self.Wr = nn.Linear(2 * d + fin, d)
            self.Wn = nn.Linear(2 * d + fin, d)
            for lin in (self.Wz, self.Wr, self.Wn):
                with torch.no_grad():
                    w = torch.empty(d, 2 * d + fin)
                    nn.init.orthogonal_(w[:, :d])
                    nn.init.orthogonal_(w[:, d:2 * d])
                    nn.init.xavier_uniform_(w[:, 2 * d:])
                    lin.weight.copy_(w)
                    lin.bias.zero_()

        def forward(self, y, H, S):  # noqa: ANN001, ANN202 - y (B,N,fin) H (B,N,d) S (B,N,N)
            SH = torch.bmm(S, H)
            inp = torch.cat([H, SH, y], -1)
            z = torch.sigmoid(self.Wz(inp))
            rr = torch.sigmoid(self.Wr(inp))
            rH = rr * H
            n = torch.tanh(self.Wn(torch.cat([rH, torch.bmm(S, rH), y], -1)))
            return (1 - z) * H + z * n

    class Head(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.mu = nn.Linear(d, 1)
            self.ld = nn.Linear(d, 1)
            self.L = nn.Linear(d, r) if r else None
            for lin in [self.mu, self.ld] + ([self.L] if r else []):
                nn.init.xavier_uniform_(lin.weight)
                nn.init.zeros_(lin.bias)

        def forward(self, H):  # noqa: ANN001, ANN202
            mu = self.mu(H)[..., 0]
            dd = torch.clamp(torch.exp(self.ld(H)[..., 0]), min=EPS_D)
            L = self.L(H) if r else H[..., :0]
            return mu, dd, L

    class Net(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.cell = Cell()
            self.head = Head()

    return Net()


def _nll_lowrank(torch, y, mu, dd, L, node_mask):  # noqa: ANN001, ANN202
    """Per-sample Gaussian NLL (B,), masked nodes contribute nothing (their d = 1, L = 0, r = 0)."""
    m = node_mask
    dd = torch.where(m, dd, torch.ones_like(dd))
    L = L * m[..., None]
    rr = (y - mu) * m
    dinv = 1.0 / dd
    rk = L.shape[-1]
    quad = (rr * rr * dinv).sum(-1)
    ld = torch.log(dd).sum(-1)
    if rk:
        DL = L * dinv[..., None]
        M = torch.eye(rk, dtype=y.dtype).expand(y.shape[0], rk, rk) + torch.bmm(L.transpose(1, 2), DL)
        C = torch.linalg.cholesky(M)
        u = torch.bmm(L.transpose(1, 2), (dinv * rr)[..., None])
        v = torch.cholesky_solve(u, C)
        quad = quad - (u * v).sum((-1, -2))
        ld = ld + 2.0 * torch.log(torch.diagonal(C, dim1=-2, dim2=-1)).sum(-1)
    return 0.5 * (quad + ld)


# ------------------------------------------------------------------------------------------------ shared training


@dataclass
class GraphSeq:
    """One group's sequence for the GCN-GRU: X (T, N) returns (NaN rows = no trading for the group), A (N, N)."""
    X: np.ndarray
    A: np.ndarray
    scale: np.ndarray | None = None       # per-node pretrain sd (set by train_gcn_gru)


@dataclass
class GCNGRUModel:
    state: dict[str, Any]
    d: int = 32
    r: int = 4
    fin: int = 2
    meta: dict[str, Any] = field(default_factory=dict)

    def net(self):  # noqa: ANN201
        torch = _torch()
        net = _build_modules(self.d, self.fin, self.r)
        net.load_state_dict({k: torch.as_tensor(v) for k, v in self.state.items()})
        net.eval()
        return net

    def save(self, path: str) -> None:
        np.savez_compressed(path, **{f"w__{k}": v for k, v in self.state.items()},
                            cfg=np.array([self.d, self.r, self.fin]))

    @classmethod
    def load(cls, path: str) -> GCNGRUModel:
        z = np.load(path)
        d, r, fin = (int(x) for x in z["cfg"])
        return cls(state={k[3:]: z[k] for k in z.files if k.startswith("w__")}, d=d, r=r, fin=fin)


def _features(torch, Xs):  # noqa: ANN001, ANN202 - Xs (B,T,N) standardised
    return torch.stack([Xs, Xs.abs()], -1)


def _pad(seqs: list[GraphSeq], T: int | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """-> Xs (B,T,N) standardised (NaN -> 0), valid (B,T) all-node rows, node_mask (B,N), S (B,N,N)."""
    B = len(seqs)
    N = max(s.X.shape[1] for s in seqs)
    T = T or max(s.X.shape[0] for s in seqs)
    Xs = np.zeros((B, T, N), dtype=np.float32)
    valid = np.zeros((B, T), dtype=bool)
    nm = np.zeros((B, N), dtype=bool)
    S = np.zeros((B, N, N), dtype=np.float32)
    for b, s in enumerate(seqs):
        t, n = s.X.shape
        t = min(t, T)
        z = s.X[:t] / s.scale  # type: ignore[operator]
        ok = np.isfinite(z).all(axis=1)
        Xs[b, :t, :n] = np.where(np.isfinite(z), z, 0.0)
        valid[b, :t] = ok
        nm[b, :n] = True
        S[b, :n, :n] = graph_shift(s.A)
    return Xs, valid, nm, S


def train_gcn_gru(seqs: list[GraphSeq], epochs: int = 30, d: int = 32, r: int = 4, lr: float = 1e-3,
                  weight_decay: float = 1e-4, bptt: int = 64, burn_frac: float = 0.10, clip: float = 5.0,
                  seed: int = 0, batch_groups: int = 64, log: Any = None, time_budget_s: float | None = None) -> GCNGRUModel:
    """Fit one node-shared GCN-GRU on the PRETRAIN sequences of several groups (teacher forcing, TBPTT)."""
    torch = _torch()
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    for s in seqs:
        sd = np.nanstd(s.X, axis=0)
        s.scale = np.where(np.isfinite(sd) & (sd > 0), sd, 1.0)
    net = _build_modules(d, 2, r)
    opt = torch.optim.Adam(net.parameters(), lr=lr, betas=(0.9, 0.999), weight_decay=weight_decay)
    t_start = time.time()
    hist = []
    order = np.arange(len(seqs))
    for ep in range(epochs):
        rng.shuffle(order)
        ep_loss, ep_n = 0.0, 0
        for b0 in range(0, len(order), batch_groups):
            batch = [seqs[i] for i in order[b0:b0 + batch_groups]]
            Xs, valid, nm, S = _pad(batch)
            Xt = torch.from_numpy(Xs)
            Vt = torch.from_numpy(valid)
            Mt = torch.from_numpy(nm)
            St = torch.from_numpy(S)
            B, T, N = Xs.shape
            nvalid = Mt.sum(-1).clamp(min=1).float()
            burn = max(int(burn_frac * T), 5)
            H = torch.zeros(B, N, d)
            feats = _features(torch, Xt)
            loss, cnt = 0.0, 0
            for t in range(T - 1):
                Hn = net.cell(feats[:, t], H, St)
                H = torch.where(Vt[:, t, None, None], Hn, H)
                if t >= burn:
                    mu, dd, L = net.head(H)
                    ok = Vt[:, t] & Vt[:, t + 1]
                    if ok.any():
                        nll = _nll_lowrank(torch, Xt[:, t + 1], mu, dd, L, Mt) / nvalid
                        loss = loss + (nll * ok).sum() / ok.sum()
                        cnt += 1
                if (t + 1) % bptt == 0 or t == T - 2:
                    if cnt:
                        lt = loss / cnt
                        opt.zero_grad()
                        lt.backward()
                        torch.nn.utils.clip_grad_norm_(net.parameters(), clip)
                        opt.step()
                        ep_loss += float(lt.detach()) * cnt
                        ep_n += cnt
                    H = H.detach()
                    loss, cnt = 0.0, 0
        hist.append(ep_loss / max(ep_n, 1))
        if log:
            log(f"gcn_gru epoch {ep + 1}/{epochs} nll/node {hist[-1]:.4f} ({time.time() - t_start:.0f}s)")
        if time_budget_s and time.time() - t_start > time_budget_s:
            break
    state = {k: v.detach().numpy().copy() for k, v in net.state_dict().items()}
    return GCNGRUModel(state=state, d=d, r=r, fin=2, meta={"epochs_run": len(hist), "loss": hist,
                                                           "seconds": round(time.time() - t_start, 1)})


def emit_gcn_gru(model: GCNGRUModel, X: np.ndarray, A: np.ndarray, scale: np.ndarray) -> tuple[np.ndarray, Cov]:
    """Run the frozen model over X (T, N) from H_0 = 0; row t = law of X[t + 1] given X[: t + 1] (original units)."""
    torch = _torch()
    net = model.net()
    seq = GraphSeq(X=np.asarray(X, dtype=np.float64), A=A, scale=scale)
    Xs, valid, nm, S = _pad([seq])
    T, N = X.shape
    mus = np.empty((T, N))
    ds = np.empty((T, N))
    Ls = np.empty((T, N, model.r))
    with torch.no_grad():
        Xt = torch.from_numpy(Xs)
        feats = _features(torch, Xt)
        St = torch.from_numpy(S)
        H = torch.zeros(1, N, model.d)
        for t in range(T):
            if valid[0, t]:
                H = net.cell(feats[:, t], H, St)
            mu, dd, L = net.head(H)
            mus[t], ds[t], Ls[t] = mu[0].numpy(), dd[0].numpy(), L[0].numpy()
    return mus * scale, Cov(d=ds * scale ** 2, L=Ls * scale[None, :, None])


@dataclass
class GCNGRUFilter(Filter):
    """Single-group Filter interface around the shared model (fit trains on this group only)."""
    A: np.ndarray | None = None
    epochs: int = 30
    d: int = 32
    r: int = 4
    seed: int = 0
    model: GCNGRUModel | None = None
    scale: np.ndarray | None = None
    name: str = "gcn_gru"

    def fit(self, X: np.ndarray, Y: np.ndarray | None = None) -> GCNGRUFilter:
        X = np.asarray(X, dtype=np.float64)
        A = self.A if self.A is not None else np.zeros((X.shape[1], X.shape[1]))
        seq = GraphSeq(X=X, A=A)
        self.model = train_gcn_gru([seq], epochs=self.epochs, d=self.d, r=self.r, seed=self.seed)
        self.scale = seq.scale
        return self

    def emit(self, X: np.ndarray) -> tuple[np.ndarray, Cov]:
        A = self.A if self.A is not None else np.zeros((X.shape[1], X.shape[1]))
        return emit_gcn_gru(self.model, X, A, self.scale)  # type: ignore[arg-type]

    def params(self) -> dict[str, Any]:
        return {"d": self.d, "r": self.r, "epochs": self.epochs, **(self.model.meta if self.model else {})}


# ------------------------------------------------------------------------------------------------ horizon GRU (A)


HORIZONS = (1, 5, 10, 20)


def horizon_cov_from_segments(v_seg: np.ndarray, horizons: tuple[int, ...] = HORIZONS) -> np.ndarray:
    """Per-day variances on the horizon segments (1, 2-5, 6-10, 11-20 for horizons 1/5/10/20) -> cumulative
    V(h) and the covariance Sigma_ij = V(min(h_i, h_j)) of overlapping forward log returns (random-walk structure
    with a time-varying variance term structure). v_seg (..., K) -> Sigma (..., K, K)."""
    lens = np.diff(np.concatenate([[0], horizons]))
    V = np.cumsum(v_seg * lens, axis=-1)
    K = len(horizons)
    idx = np.minimum.outer(np.arange(K), np.arange(K))
    return V[..., idx]


def _build_horizon_net(fin: int, hidden: int, K: int):  # noqa: ANN202
    torch = _torch()
    nn = torch.nn

    class HNet(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.gru = nn.GRU(fin, hidden, batch_first=True)
            self.head = nn.Linear(hidden, K)
            nn.init.zeros_(self.head.bias)

        def forward(self, x, h=None):  # noqa: ANN001, ANN202
            o, h = self.gru(x, h)
            return self.head(o), h

    return HNet()


@dataclass
class HorizonGRU:
    """Learned envelope head: GRU over per-security daily features -> log per-day variances of the horizon segments,
    plus a log-variance offset relative to the EWMA variance (inputs/outputs are in units of the security's EWMA
    daily variance, so one model serves every security). Mean fixed at 0. Gaussian NLL of the 4-vector of overlapping
    forward log returns, trained jointly over securities (batch), TBPTT 64."""
    hidden: int = 16
    epochs: int = 15
    horizons: tuple[int, ...] = HORIZONS
    state: dict[str, Any] | None = None
    fin: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    def fit(self, F: np.ndarray, Y: np.ndarray, base_var: np.ndarray, valid: np.ndarray, seed: int = 0,
            bptt: int = 64, lr: float = 3e-3, log: Any = None) -> HorizonGRU:
        """F (B, T, fin) features known at t; Y (B, T, K) forward log returns from t; base_var (B, T) EWMA daily
        variance known at t; valid (B, T) rows whose Y is fully realised inside the pretrain block."""
        torch = _torch()
        torch.manual_seed(seed)
        B, T, fin = F.shape
        K = len(self.horizons)
        self.fin = fin
        net = _build_horizon_net(fin, self.hidden, K)
        opt = torch.optim.Adam(net.parameters(), lr=lr, weight_decay=1e-4)
        Ft = torch.from_numpy(np.nan_to_num(F, nan=0.0).astype(np.float32))
        sc = np.sqrt(np.maximum(base_var, 1e-12))[..., None]
        Yt = torch.from_numpy(np.nan_to_num(Y / sc, nan=0.0).astype(np.float32))
        Vt = torch.from_numpy(valid & np.isfinite(Y).all(-1))
        lens = torch.tensor(np.diff(np.concatenate([[0], self.horizons])), dtype=torch.float32)
        idx = torch.from_numpy(np.minimum.outer(np.arange(K), np.arange(K)))
        t0 = time.time()
        hist = []
        for ep in range(self.epochs):
            h = None
            tot, n = 0.0, 0
            for a in range(0, T, bptt):
                b = min(T, a + bptt)
                out, h = net(Ft[:, a:b], h)
                h = h.detach()
                v = torch.exp(out).clamp(min=1e-4)                     # per-day variance multipliers
                V = torch.cumsum(v * lens, -1)
                S = V[..., idx] + 1e-4 * torch.eye(K)
                m = Vt[:, a:b]
                if not m.any():
                    continue
                Sm, ym = S[m], Yt[:, a:b][m]
                C = torch.linalg.cholesky(Sm)
                z = torch.cholesky_solve(ym[..., None], C)
                nll = 0.5 * ((ym[..., None] * z).sum((-1, -2)) + 2 * torch.log(torch.diagonal(C, dim1=-2, dim2=-1)).sum(-1))
                loss = nll.mean()
                opt.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(net.parameters(), 5.0)
                opt.step()
                tot += float(loss.detach()) * int(m.sum())
                n += int(m.sum())
            hist.append(tot / max(n, 1))
            if log:
                log(f"horizon_gru epoch {ep + 1}/{self.epochs} nll {hist[-1]:.4f} ({time.time() - t0:.0f}s)")
        self.state = {k: v.detach().numpy().copy() for k, v in net.state_dict().items()}
        self.meta = {"loss": hist, "seconds": round(time.time() - t0, 1)}
        return self

    def emit(self, F: np.ndarray, base_var: np.ndarray) -> np.ndarray:
        """-> Sigma (B, T, K, K) in return units (row t: law of the forward vector from t)."""
        torch = _torch()
        K = len(self.horizons)
        net = _build_horizon_net(self.fin, self.hidden, K)
        net.load_state_dict({k: torch.as_tensor(v) for k, v in self.state.items()})  # type: ignore[union-attr]
        net.eval()
        with torch.no_grad():
            out, _ = net(torch.from_numpy(np.nan_to_num(F, nan=0.0).astype(np.float32)))
        v = np.maximum(np.exp(out.numpy().astype(np.float64)), 1e-4)
        S = horizon_cov_from_segments(v, self.horizons) + 1e-4 * np.eye(K)
        return S * np.maximum(base_var, 1e-12)[..., None, None]

    def save(self, path: str) -> None:
        np.savez_compressed(path, **{f"w__{k}": v for k, v in (self.state or {}).items()},
                            cfg=np.array([self.hidden, self.fin, *self.horizons]))

    @classmethod
    def load(cls, path: str) -> HorizonGRU:
        z = np.load(path)
        cfg = [int(x) for x in z["cfg"]]
        return cls(hidden=cfg[0], fin=cfg[1], horizons=tuple(cfg[2:]),
                   state={k[3:]: z[k] for k in z.files if k.startswith("w__")})


def n_params(model: GCNGRUModel) -> int:
    return int(sum(np.prod(v.shape) for v in model.state.values()))
