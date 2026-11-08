"""Multi-factor Transformer: FT-Transformer-style feature tokenizer + a cross-sectional peer-attention pass +
a short temporal context, combined in one small model (PyTorch, CPU).

Architecture (one forward pass per row, batched per rebalance date so peer attention has the right "set"):
  1. Feature tokenizer: each of the F live numeric features is a scalar -> linear projection to d_model, plus a
     learned per-feature-id embedding (this is the "FT-Transformer" part: tabular features treated as a token
     sequence rather than one concatenated vector).
  2. A learned sector-id embedding is appended as one more token ("sector token"), so the self-attention over
     [CLS, feature_1..F, sector] lets the row's own representation already account for which sector it's in.
  3. A small `nn.TransformerEncoder` (1-2 layers) runs over that per-row token sequence; the CLS output is the
     row representation.
  4. Short temporal context: the same tokenizer (mean-pooled) is applied to the security's feature snapshot from
     1 and 2 rebalance dates back (`x_lag1`, `x_lag2`), each tagged with a distinct learned time embedding, and
     added into the row representation. This is a lighter-weight substitute for a full temporal transformer
     sub-module (scope cut documented in the training job's `model_runs.notes`), but it does give the model
     explicit access to very recent feature trajectory (e.g. "momentum accelerating vs 2 months ago"), not just
     the current snapshot.
  5. Cross-sectional peer attention: all rows sharing the same rebalance date are attended against each other
     (`nn.MultiheadAttention` with the whole date's universe as one sequence) -- "each date's universe as a set".
  6. A 2-layer MLP head maps the fused representation to a scalar score.

Missing feature values (NaN, e.g. a pending-table feature, or a name too young for a 252d momentum window) are
filled with 0 after the cross-sectional rank-gauss transform (where 0 is the transform's own neutral/median
value) -- a simplification vs. a learned missingness embedding, documented here rather than silently assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import torch
from torch import nn

torch.set_num_threads(8)  # see models/xgb.py DEFAULT_PARAMS note: this box runs several agents' jobs concurrently


@dataclass
class FTConfig:
    n_features: int
    n_sectors: int
    d_model: int = 32
    n_heads: int = 4
    n_layers: int = 2
    dropout: float = 0.1


class FTTransformerCS(nn.Module):
    def __init__(self, cfg: FTConfig):
        super().__init__()
        self.cfg = cfg
        d = cfg.d_model
        self.value_proj = nn.Linear(1, d)
        self.feature_id_embed = nn.Embedding(cfg.n_features, d)
        self.sector_embed = nn.Embedding(cfg.n_sectors + 1, d)  # last index = unknown sector
        self.time_embed = nn.Embedding(3, d)  # 1 = t-1, 2 = t-2 (index 0 unused)
        self.cls = nn.Parameter(torch.zeros(1, 1, d))
        nn.init.normal_(self.cls, std=0.02)
        enc_layer = nn.TransformerEncoderLayer(d, cfg.n_heads, dim_feedforward=4 * d, dropout=cfg.dropout,
                                               batch_first=True, activation="gelu")
        self.encoder = nn.TransformerEncoder(enc_layer, cfg.n_layers)
        self.peer_attn = nn.MultiheadAttention(d, cfg.n_heads, dropout=cfg.dropout, batch_first=True)
        self.peer_ln = nn.LayerNorm(d)
        self.head = nn.Sequential(nn.Linear(d, d), nn.GELU(), nn.Dropout(cfg.dropout), nn.Linear(d, 1))

    def _tokenize(self, x: torch.Tensor) -> torch.Tensor:
        tok = self.value_proj(x.unsqueeze(-1))  # (B, F, d)
        idx = torch.arange(x.shape[1], device=x.device)
        return tok + self.feature_id_embed(idx)

    def row_repr(self, x: torch.Tensor, x_lag1: torch.Tensor, x_lag2: torch.Tensor,
                sector_idx: torch.Tensor) -> torch.Tensor:
        b = x.shape[0]
        tok = self._tokenize(x)
        sec_tok = self.sector_embed(sector_idx).unsqueeze(1)
        cls = self.cls.expand(b, -1, -1)
        enc = self.encoder(torch.cat([cls, tok, sec_tok], dim=1))
        row = enc[:, 0, :]
        t1 = torch.full((b,), 1, device=x.device, dtype=torch.long)
        t2 = torch.full((b,), 2, device=x.device, dtype=torch.long)
        lag1 = self._tokenize(x_lag1).mean(dim=1) + self.time_embed(t1)
        lag2 = self._tokenize(x_lag2).mean(dim=1) + self.time_embed(t2)
        # TODO(leakage audit, 2026-10-11): this unconditional sum lets real (non-leaked) lag-feature
        # information dominate the output almost independently of the learned `enc` path / training target --
        # tests/ml/test_leakage.py::test_transformer_permutation_leakage_check_passes (currently xfail) found
        # that training on a *permuted* rank_label still yields ~+0.07-0.09 OOS rank-IC against the true
        # labels, unaffected by epoch count (4 vs 12), which rules out early-stopping-on-noise as the cause.
        # Candidate fixes: a learned gate on lag1/lag2 (e.g. a small MLP/sigmoid instead of a bare sum), or
        # concatenating instead of summing so the encoder must explicitly attend to the lag tokens. Until one
        # of those is implemented and the xfail test flips to passing, this model must not be promoted to
        # production (see app.ml.ensemble.apply_production_gate's requirement to also pass the permutation
        # test, and docs/CONTRACT_CHANGES.md).
        return row + lag1 + lag2

    def forward(self, x: torch.Tensor, x_lag1: torch.Tensor, x_lag2: torch.Tensor,
               sector_idx: torch.Tensor) -> torch.Tensor:
        row = self.row_repr(x, x_lag1, x_lag2, sector_idx)
        q = row.unsqueeze(0)  # whole date-batch as one sequence (seq_len = universe size that date)
        peer_out, _ = self.peer_attn(q, q, q)
        row = self.peer_ln(row + peer_out.squeeze(0))
        return self.head(row).squeeze(-1)


@dataclass
class DateBatch:
    date: Any
    x: np.ndarray
    x_lag1: np.ndarray
    x_lag2: np.ndarray
    sector_idx: np.ndarray
    y: np.ndarray
    security_ids: list[str] = field(default_factory=list)


def _to_tensors(b: DateBatch) -> tuple[torch.Tensor, ...]:
    return (torch.from_numpy(b.x).float(), torch.from_numpy(b.x_lag1).float(),
           torch.from_numpy(b.x_lag2).float(), torch.from_numpy(b.sector_idx).long(),
           torch.from_numpy(b.y).float())


def train_model(train_batches: list[DateBatch], val_batches: list[DateBatch], n_features: int, n_sectors: int,
                max_epochs: int = 12, patience: int = 3, lr: float = 1e-3, seed: int = 42,
                rank_ic_fn: Any = None) -> tuple[FTTransformerCS, dict[str, Any]]:
    torch.manual_seed(seed)
    model = FTTransformerCS(FTConfig(n_features=n_features, n_sectors=n_sectors))
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)
    loss_fn = nn.MSELoss()
    best_val, best_state, bad_epochs = -np.inf, None, 0
    history = []
    for epoch in range(max_epochs):
        model.train()
        order = np.random.default_rng(seed + epoch).permutation(len(train_batches))
        total_loss = 0.0
        for i in order:
            x, x1, x2, sec, y = _to_tensors(train_batches[i])
            opt.zero_grad()
            pred = model(x, x1, x2, sec)
            loss = loss_fn(pred, y)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            total_loss += float(loss.item())
        val_ic = _eval_rank_ic(model, val_batches) if val_batches else -total_loss
        history.append({"epoch": epoch, "train_loss": total_loss / max(1, len(train_batches)), "val_rank_ic": val_ic})
        if val_ic > best_val:
            best_val, best_state, bad_epochs = val_ic, {k: v.clone() for k, v in model.state_dict().items()}, 0
        else:
            bad_epochs += 1
            if bad_epochs >= patience:
                break
    if best_state is not None:
        model.load_state_dict(best_state)
    return model, {"history": history, "best_val_rank_ic": best_val}


@torch.no_grad()
def _eval_rank_ic(model: FTTransformerCS, batches: list[DateBatch]) -> float:
    model.eval()
    ics = []
    for b in batches:
        x, x1, x2, sec, y = _to_tensors(b)
        pred = model(x, x1, x2, sec).numpy()
        if len(pred) < 5 or np.std(pred) == 0:
            continue
        r1, r2 = _rank(pred), _rank(b.y)
        if r1.std() and r2.std():
            ics.append(float(np.corrcoef(r1, r2)[0, 1]))
    return float(np.mean(ics)) if ics else -1.0


def _rank(a: np.ndarray) -> np.ndarray:
    return np.argsort(np.argsort(a)).astype(float)


@torch.no_grad()
def predict_batches(model: FTTransformerCS, batches: list[DateBatch]) -> dict[Any, np.ndarray]:
    model.eval()
    out = {}
    for b in batches:
        x, x1, x2, sec, _ = _to_tensors(b)
        out[b.date] = model(x, x1, x2, sec).numpy()
    return out


def integrated_gradients(model: FTTransformerCS, batch: DateBatch, steps: int = 16) -> np.ndarray:
    """Approximate per-feature attribution for the *current* feature snapshot only (lag/sector contributions are
    not decomposed -- documented scope cut). Returns (N, F) contributions summing approx. to score - baseline(0)."""
    model.eval()
    x, x1, x2, sec, _ = _to_tensors(batch)
    baseline = torch.zeros_like(x)
    total_grad = torch.zeros_like(x)
    for step in range(1, steps + 1):
        alpha = step / steps
        xi = (baseline + alpha * (x - baseline)).detach().requires_grad_(True)
        pred = model(xi, x1, x2, sec)
        grad = torch.autograd.grad(pred.sum(), xi)[0]
        total_grad += grad
    avg_grad = total_grad / steps
    return ((x - baseline) * avg_grad).detach().numpy()
