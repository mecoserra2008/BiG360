"""Studio v2 S2 neural-network core (torch, CPU only, threads capped): MLP, small GRU / LSTM on look-back windows and an
autoencoder for latent factors; early stopping on a time-ordered validation block inside each training fold, seed
averaging, deterministic seeds (owner: model-studio).

Embedded verbatim in exported scripts after app/modeling/_ml.py, _causal2.py and _advml.py.
"""

from __future__ import annotations

from typing import Any, Callable

import numpy as np
import pandas as pd

try:  # inside the app; in an exported script the cores are already defined above this module
    from ._advml import inner_val_split  # noqa: F401
    from ._causal2 import n_threads  # noqa: F401
except ImportError:  # pragma: no cover
    pass

NN_VERSION = "1.0"


class TorchCtx:
    """Caps torch threads (n_threads(): the shared studio budget, max 4) and turns on deterministic kernels; restores both."""

    def __enter__(self) -> "TorchCtx":
        import torch

        self.t, self.det = torch.get_num_threads(), torch.are_deterministic_algorithms_enabled()
        torch.set_num_threads(n_threads())
        torch.use_deterministic_algorithms(True, warn_only=True)
        return self

    def __exit__(self, *a: Any) -> None:
        import torch

        torch.set_num_threads(self.t)
        torch.use_deterministic_algorithms(self.det, warn_only=True)


def _act(name: str) -> Any:
    import torch.nn as nn

    return {"relu": nn.ReLU, "tanh": nn.Tanh, "gelu": nn.GELU}[name]()


def mlp_net(n_in: int, hidden: list[int], dropout: float, activation: str, n_out: int = 1) -> Any:
    import torch.nn as nn

    layers, k = [], n_in
    for h in hidden:
        layers += [nn.Linear(k, h), _act(activation)] + ([nn.Dropout(dropout)] if dropout > 0 else [])
        k = h
    layers.append(nn.Linear(k, n_out))
    return nn.Sequential(*layers)


def rnn_net(n_in: int, cell: str, hidden: int, layers: int, dropout: float) -> Any:
    import torch.nn as nn

    class RNN(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            R = nn.GRU if cell == "gru" else nn.LSTM
            self.rnn = R(n_in, hidden, num_layers=layers, batch_first=True, dropout=dropout if layers > 1 else 0.0)
            self.head = nn.Linear(hidden, 1)

        def forward(self, x: Any) -> Any:
            out, _ = self.rnn(x)
            return self.head(out[:, -1])

    return RNN()


def ae_net(n_in: int, k: int, hidden: int, activation: str) -> Any:
    import torch.nn as nn

    class AE(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.enc = nn.Sequential(nn.Linear(n_in, hidden), _act(activation), nn.Linear(hidden, k))
            self.dec = nn.Sequential(nn.Linear(k, hidden), _act(activation), nn.Linear(hidden, n_in))

        def forward(self, x: Any) -> Any:
            return self.dec(self.enc(x))

    return AE()


def train(net: Any, Xtr: np.ndarray, ytr: np.ndarray, Xva: np.ndarray | None, yva: np.ndarray | None, loss: str,
          lr: float, wd: float, batch: int, max_epochs: int, patience: int, seed: int,
          check: Callable[[], None] | None = None) -> tuple[Any, dict[str, list[float]], int]:
    """Adam + mini-batches (seeded shuffling); keeps the weights of the epoch with the lowest validation loss and stops
    after `patience` epochs without improvement. Returns (net, {train, valid} loss per epoch, best epoch)."""
    import torch

    g = torch.Generator().manual_seed(seed)
    xt, yt = torch.as_tensor(Xtr, dtype=torch.float32), torch.as_tensor(ytr, dtype=torch.float32)
    xv = torch.as_tensor(Xva, dtype=torch.float32) if Xva is not None and len(Xva) else None
    yv = torch.as_tensor(yva, dtype=torch.float32) if xv is not None else None
    lf = torch.nn.BCEWithLogitsLoss() if loss == "bce" else torch.nn.MSELoss()
    opt = torch.optim.Adam(net.parameters(), lr=lr, weight_decay=wd)
    best, best_ep, wait, state = np.inf, 0, 0, None
    curve: dict[str, list[float]] = {"train": [], "valid": []}
    n = len(xt)
    for ep in range(max_epochs):
        if check:
            check()
        net.train()
        perm = torch.randperm(n, generator=g)
        tot = 0.0
        for i in range(0, n, batch):
            b = perm[i:i + batch]
            opt.zero_grad()
            out = net(xt[b]).reshape(yt[b].shape)
            ls = lf(out, yt[b])
            ls.backward()
            opt.step()
            tot += ls.item() * len(b)
        curve["train"].append(tot / n)
        if xv is None:
            continue
        net.eval()
        with torch.no_grad():
            vl = float(lf(net(xv).reshape(yv.shape), yv))
        curve["valid"].append(vl)
        if vl < best - 1e-7:
            best, best_ep, wait = vl, ep + 1, 0
            state = {k: v.detach().clone() for k, v in net.state_dict().items()}
        else:
            wait += 1
            if wait >= patience:
                break
    if state is not None:
        net.load_state_dict(state)
    net.eval()
    return net, curve, best_ep or len(curve["train"])


def _out(net: Any, X: np.ndarray) -> np.ndarray:
    import torch

    with torch.no_grad():
        return net(torch.as_tensor(X, dtype=torch.float32)).numpy()


def standardize(Xfit: np.ndarray, *others: np.ndarray) -> list[np.ndarray]:
    """Scale by the fitting rows only (2D rows x features, or 3D windows x steps x features)."""
    ax = tuple(range(Xfit.ndim - 1))
    mu, sd = Xfit.mean(ax), Xfit.std(ax)
    sd = np.where(sd > 0, sd, 1.0)
    return [(a - mu) / sd for a in (Xfit, *others)]


def mean_curve(curves: list[dict[str, list[float]]]) -> dict[str, list[float]]:
    out = {}
    for k in ("train", "valid"):
        L = max((len(c[k]) for c in curves), default=0)
        A = np.full((len(curves), L), np.nan)
        for i, c in enumerate(curves):
            A[i, :len(c[k])] = c[k]
        out[k] = np.nanmean(A, 0).tolist() if L else []
    return out


def nn_cv(make: Callable[[], Any], X: np.ndarray, y: np.ndarray, dates: np.ndarray, folds: list, task: str,
          p: dict[str, Any], seed: int, purge: int, check: Callable[[], None] | None = None) -> dict[str, Any]:
    """Out-of-fold predictions of a supervised net (MLP on 2D X, RNN on 3D windows). Per fold: time-ordered fit /
    validation split, scaling and y-standardization from the fit rows, `n_seeds` nets averaged."""
    import torch

    oos = np.full(len(y), np.nan)
    curves, best_eps = [], []
    with TorchCtx():
        for tr, te in folds:
            fi, vi = inner_val_split(tr, dates, float(p["val_frac"]), purge, seed)
            if len(fi) < 20:
                fi, vi = tr, tr[:0]
            Xf, Xv, Xt = standardize(X[fi], X[vi], X[te])
            if task == "classification":
                yf, yv, mu, sd = y[fi], y[vi], 0.0, 1.0
            else:
                mu, sd = float(y[fi].mean()), float(y[fi].std() or 1.0)
                yf, yv = (y[fi] - mu) / sd, (y[vi] - mu) / sd
            preds, fold_curves = [], []
            for s in range(int(p["n_seeds"])):
                torch.manual_seed(seed + s)
                net, c, be = train(make(), Xf, yf, Xv if len(vi) else None, yv if len(vi) else None,
                                   "bce" if task == "classification" else "mse", float(p["lr"]), float(p["weight_decay"]),
                                   int(p["batch_size"]), int(p["max_epochs"]), int(p["patience"]), seed + s, check)
                o = _out(net, Xt).reshape(-1)
                preds.append(1 / (1 + np.exp(-o)) if task == "classification" else o * sd + mu)
                fold_curves.append(c)
                best_eps.append(be)
            oos[te] = np.mean(preds, 0)
            curves = fold_curves
    return {"oos": oos, "curve": mean_curve(curves), "best_epochs": best_eps}


def make_windows(df: pd.DataFrame, feats: list[str], L: int) -> tuple[np.ndarray, np.ndarray]:
    """Look-back windows: for each row with L - 1 earlier rows of the same entity (rows sorted by entity, date), the
    features of rows t-L+1..t. Returns (row positions of the window ends, array windows x L x features)."""
    keys = df["entity_id"].astype(str).to_numpy() if "entity_id" in df.columns else np.zeros(len(df), dtype=object)
    order = np.lexsort((pd.to_datetime(df["date"]).to_numpy(), keys))
    F = df[feats].to_numpy(float)[order]
    k = keys[order]
    ends, wins = [], []
    for i in range(L - 1, len(order)):
        if k[i - L + 1] != k[i]:
            continue
        w = F[i - L + 1:i + 1]
        if np.isfinite(w).all():
            ends.append(order[i])
            wins.append(w)
    if not wins:
        return np.array([], int), np.empty((0, L, len(feats)))
    return np.asarray(ends, int), np.stack(wins)


def ridge_window_cv(W: np.ndarray, y: np.ndarray, folds: list, task: str) -> np.ndarray:
    """Linear baseline with the same information set as the RNN: ridge (alpha 1) / logistic on the flattened window."""
    from sklearn.linear_model import LogisticRegression, Ridge

    F = W.reshape(len(W), -1)
    out = np.full(len(y), np.nan)
    for tr, te in folds:
        A, B = standardize(F[tr], F[te])
        if task == "classification":
            out[te] = LogisticRegression(C=1.0, max_iter=3000).fit(A, y[tr]).predict_proba(B)[:, 1]
        else:
            out[te] = Ridge(alpha=1.0).fit(A, y[tr]).predict(B)
    return out


def ae_fit(X: np.ndarray, train_idx: np.ndarray, val_idx: np.ndarray, k: int, p: dict[str, Any], seed: int,
           check: Callable[[], None] | None = None) -> dict[str, Any]:
    """Autoencoder(s) on standardized X (scaling from the training rows). Returns the averaged reconstruction of every
    row, the factor scores of the first seed's encoder and its training curve."""
    import torch

    Xs = standardize(X[train_idx], X)[1]
    recs, curves, enc = [], [], None
    with TorchCtx():
        for s in range(int(p["n_seeds"])):
            torch.manual_seed(seed + s)
            net = ae_net(X.shape[1], k, int(p["hidden"]), p["activation"])
            net, c, _ = train(net, Xs[train_idx], Xs[train_idx], Xs[val_idx] if len(val_idx) else None,
                              Xs[val_idx] if len(val_idx) else None, "mse", float(p["lr"]), float(p["weight_decay"]),
                              int(p["batch_size"]), int(p["max_epochs"]), int(p["patience"]), seed + s, check)
            recs.append(_out(net, Xs))
            curves.append(c)
            if enc is None:
                with torch.no_grad():
                    enc = net.enc(torch.as_tensor(Xs, dtype=torch.float32)).numpy()
    return {"Xs": Xs, "recon": np.mean(recs, 0), "factors": enc, "curve": mean_curve(curves)}
