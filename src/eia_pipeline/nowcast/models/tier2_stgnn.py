"""Tier 2: spatiotemporal graph network over the 452-cell citywide panel.

We give this the same information set as Tier 1: identical covariates, the identical rolling baselines, the identical control-only masking. The only thing Tier 2 adds is structure, in that predictions are coupled across space by graph convolution and across time by dilated causal convolution. That way the gap between the two tiers measures the graph and nothing else.

It is deliberately not autoregressive, and that is the load-bearing choice in this module. A classic traffic STGNN predicts speed at t from speed at t-1..t-T. Doing that here would be fatal, because during a game the recent hours already contain the game's effect, so the model would predict the inflated level, the residual would collapse toward zero, and the measurement would destroy itself. The model only sees calendar, weather, cell statics and a baseline built exclusively from prior control days. It never sees recent raw activity.

On memory: most of our covariates are global, meaning the same for every cell at a given hour, so materialising a full [T, N, F] array would waste about 6x. We split them instead:

    x_nt  [T, N, 4]   per-cell time-varying  (the three baselines, n_poi_live)  ~190 MB
    x_g   [T, F_g]    global time-varying    (calendar, weather)                  ~1 MB
    x_s   [N, F_s]    per-cell static        (n_poi, food_share, geometry)         tiny

The broadcast happens inside forward(). That keeps the working set small enough to be comfortable on an 8 GB laptop, which is where this actually runs.

The graph convolution keeps separate self and neighbour weights:

    H' = act( H W_self + A_hat H W_neigh )

so the model can drive W_neigh toward zero if the graph carries no information. Given that we measured citywide adjacent-cell correlation at only about 0.114, that is a real possibility, and the architecture has to be able to express it. Otherwise a useless graph would be forced to inject noise and we would misread the ablation.
"""
from __future__ import annotations

import numpy as np
import polars as pl
import torch
import torch.nn as nn

EDGE_FILES = {
    "contiguity": "data/bronze_sf/edges_contiguity.parquet",
    "distance": "data/bronze_sf/edges_distance.parquet",
    "flow": "data/bronze_sf/edges_flow.parquet",
}

GLOBAL_FEATS = ["temp_hr", "prcp_hr", "wind_hr", "t_index", "us_federal_holiday"]
# Three lookback depths, matching Tier 1 exactly so the ablation still isolates
# the graph rather than the feature set (see transform.features).
NODE_TIME_FEATS = ["base_k2", "base_k4", "base_cap120", "n_poi_live"]
STATIC_FEATS = ["n_poi", "food_share", "dist_venue_m"]

WINDOW = 48        # hours per sample
CONTEXT = 24       # leading hours used only as convolution context, not scored
STRIDE = 24


def device() -> torch.device:
    """Apple Metal when available, else CPU.

    The whole model is ~63k parameters over 452 nodes, so MPS turns a ~2 min/epoch
    CPU run into ~26 s. No CUDA branch: this is built to run on the laptop that
    holds the data (see the memory-layout note in the module docstring).
    """
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def build_tensors():
    """Return dense arrays plus index bookkeeping. Built once, reused per ablation."""
    from .tier1_gbm import load

    df = load("clean_control_strict").sort(["date", "hour", "unit_id"])
    units = df["unit_id"].unique().sort().to_list()
    uidx = {u: i for i, u in enumerate(units)}
    N = len(units)

    ts = df.select(["date", "hour"]).unique().sort(["date", "hour"])
    T = ts.height
    day0 = ts["date"].min()

    # _t arithmetically, NOT via a dict lookup: a Python map_elements over 11.9M
    # rows costs minutes, this costs milliseconds. The panel is a dense spine, so
    # (days since start) * 24 + hour is exact by construction.
    df = df.with_columns(
        pl.col("unit_id").replace_strict(uidx, return_dtype=pl.Int32).alias("_n"),
        ((pl.col("date") - pl.lit(day0)).dt.total_days() * 24
         + pl.col("hour")).cast(pl.Int32).alias("_t"),
    )
    assert df["_t"].max() == T - 1, f"timestamp index {df['_t'].max()} != {T - 1}"

    y = np.zeros((T, N), dtype=np.float32)
    mask = np.zeros((T, N), dtype=bool)
    x_nt = np.zeros((T, N, len(NODE_TIME_FEATS)), dtype=np.float32)
    t_arr, n_arr = df["_t"].to_numpy(), df["_n"].to_numpy()

    y[t_arr, n_arr] = df["y"].to_numpy()
    mask[t_arr, n_arr] = df["is_control"].to_numpy()
    for k, f in enumerate(NODE_TIME_FEATS):
        v = df[f].fill_null(strategy="zero").to_numpy().astype(np.float32)
        x_nt[t_arr, n_arr, k] = v

    # global (per-hour) block, taken from the first cell of each timestamp
    g = df.unique(subset=["_t"], keep="first").sort("_t")
    x_g = np.stack([g[f].fill_null(0).to_numpy().astype(np.float32)
                    for f in GLOBAL_FEATS], axis=1)
    hh = g["hour"].to_numpy().astype(np.float32)
    dd = g["dow"].to_numpy().astype(np.float32)
    mm = g["month"].to_numpy().astype(np.float32)
    cyc = np.stack([np.sin(2 * np.pi * hh / 24), np.cos(2 * np.pi * hh / 24),
                    np.sin(2 * np.pi * dd / 7), np.cos(2 * np.pi * dd / 7),
                    np.sin(2 * np.pi * mm / 12), np.cos(2 * np.pi * mm / 12)], axis=1)
    x_g = np.concatenate([x_g, cyc.astype(np.float32)], axis=1)

    s = df.unique(subset=["_n"], keep="first").sort("_n")
    x_s = np.stack([s[f].to_numpy().astype(np.float32) for f in STATIC_FEATS], axis=1)
    br = np.deg2rad(s["bearing_venue_deg"].to_numpy().astype(np.float32))
    x_s = np.concatenate([x_s, np.stack([np.sin(br), np.cos(br)], axis=1)], axis=1)

    split = g["split"].to_list()
    return dict(y=y, mask=mask, x_nt=x_nt, x_g=x_g, x_s=x_s,
                units=units, uidx=uidx, T=T, N=N, split=split, ts=ts)


def adjacency(edge_key: str, uidx: dict, N: int, self_loops: bool = True) -> torch.Tensor:
    """Symmetrically normalised dense adjacency. N=452, so dense is 204k floats."""
    if edge_key == "none":
        return torch.zeros((N, N), dtype=torch.float32)
    e = pl.read_parquet(EDGE_FILES[edge_key])
    A = np.zeros((N, N), dtype=np.float32)
    for row in e.iter_rows(named=True):
        a, b = uidx.get(row["src"]), uidx.get(row["dst"])
        if a is None or b is None:
            continue
        A[a, b] = float(row["w"])
    A = np.maximum(A, A.T)
    if self_loops:
        np.fill_diagonal(A, 1.0)
    d = A.sum(1)
    d[d == 0] = 1.0
    Dm = 1.0 / np.sqrt(d)
    A = A * Dm[:, None] * Dm[None, :]
    return torch.from_numpy(A)


class GraphConv(nn.Module):
    """H' = H W_self + A_hat H W_neigh. W_neigh can learn to vanish."""

    def __init__(self, c: int):
        super().__init__()
        self.self_w = nn.Linear(c, c)
        self.neigh_w = nn.Linear(c, c, bias=False)

    def forward(self, h: torch.Tensor, A: torch.Tensor) -> torch.Tensor:
        """[B,T,N,C] -> [B,T,N,C]. Mixes each node with its neighbours.

        The einsum applies A across the NODE axis only, independently at every
        timestep, deliberately, since lag-1 spatial correlation was measured at or
        below contemporaneous, so there is no travelling wave to model.
        """
        agg = torch.einsum("nm,btmc->btnc", A, h)
        return self.self_w(h) + self.neigh_w(agg)


class TemporalConv(nn.Module):
    """Causal dilated 1-D convolution along time, weights shared across cells."""

    def __init__(self, c: int, dilation: int, k: int = 3):
        super().__init__()
        self.pad = (k - 1) * dilation
        self.conv = nn.Conv1d(c, c, k, dilation=dilation)

    def forward(self, h: torch.Tensor) -> torch.Tensor:
        """[B,T,N,C] -> [B,T,N,C]. Convolves along TIME, shared across nodes.

        Nodes are folded into the batch so one Conv1d serves all 452 cells. Padding
        is left-side only, so position t never sees t+1, which is required because the
        model must not peek at hours it is meant to predict.
        """
        B, T, N, C = h.shape
        x = h.permute(0, 2, 3, 1).reshape(B * N, C, T)
        x = self.conv(nn.functional.pad(x, (self.pad, 0)))
        return x.reshape(B, N, C, T).permute(0, 3, 1, 2)


class STGNN(nn.Module):
    """Node embedding + dilated temporal conv + graph conv, per (cell, hour).

    Each block is residual: h = LayerNorm(h + act(GraphConv(TemporalConv(h)))), so
    with the graph zeroed the network degrades gracefully to a per-cell temporal
    model rather than breaking, which is exactly what the `none` ablation arm
    needs in order to be a fair control.

    Covariates arrive in three shapes and are broadcast to [B,T,N,*] inside
    forward(): per-cell-per-hour, per-hour (global), and per-cell (static). See the
    module docstring for why they are stored separately.
    """

    def __init__(self, N: int, f_nt: int, f_g: int, f_s: int,
                 d_emb: int = 32, hidden: int = 64, blocks: int = 2,
                 dropout: float = 0.0):
        super().__init__()
        self.drop = nn.Dropout(dropout)
        self.emb = nn.Embedding(N, d_emb)
        self.inp = nn.Linear(f_nt + f_g + f_s + d_emb, hidden)
        self.tconv = nn.ModuleList([TemporalConv(hidden, 2 ** i) for i in range(blocks)])
        self.gconv = nn.ModuleList([GraphConv(hidden) for _ in range(blocks)])
        self.norm = nn.ModuleList([nn.LayerNorm(hidden) for _ in range(blocks)])
        self.act = nn.GELU()
        self.head = nn.Sequential(nn.Linear(hidden, hidden), nn.GELU(), nn.Linear(hidden, 1))

    def forward(self, x_nt, x_g, x_s, A):
        """-> [B,T,N] predicted log1p(person_hours), one scalar per cell-hour."""
        B, T, N, _ = x_nt.shape
        g = x_g.unsqueeze(2).expand(B, T, N, x_g.shape[-1])
        s = x_s.unsqueeze(0).unsqueeze(0).expand(B, T, N, x_s.shape[-1])
        e = self.emb.weight.unsqueeze(0).unsqueeze(0).expand(B, T, N, -1)
        h = self.act(self.inp(torch.cat([x_nt, g, s, e], dim=-1)))
        for tc, gc, nm in zip(self.tconv, self.gconv, self.norm):
            h = h + self.drop(self.act(gc(tc(h), A)))
            h = nm(h)
        return self.head(h).squeeze(-1)


def _windows(split: list, T: int, stride: int = STRIDE):
    """Window start indices grouped by the split of their SCORED hours.

    Each window is WINDOW hours long but only the trailing WINDOW-CONTEXT hours
    are scored; the leading CONTEXT hours exist purely to fill the causal
    convolution's receptive field. A window is assigned to a split only if every
    scored hour belongs to it, so no window straddles a boundary and no val/test
    hour can influence a training gradient.
    """
    out = {"train": [], "val": [], "test": []}
    for t0 in range(0, T - WINDOW + 1, stride):
        seg = set(split[t0 + CONTEXT: t0 + WINDOW])
        if len(seg) == 1:
            out[seg.pop()].append(t0)
    return out


def _standardise(d: dict, train_t: np.ndarray):
    """Z-score using TRAIN hours only, so val/test statistics never leak in."""
    for key, axis in (("x_nt", (0, 1)), ("x_g", (0,))):
        a = d[key]
        sub = a[train_t]
        mu, sd = sub.mean(axis=axis, keepdims=True), sub.std(axis=axis, keepdims=True)
        sd[sd == 0] = 1.0
        d[key] = ((a - mu) / sd).astype(np.float32)
    s = d["x_s"]
    mu, sd = s.mean(0, keepdims=True), s.std(0, keepdims=True)
    sd[sd == 0] = 1.0
    d["x_s"] = ((s - mu) / sd).astype(np.float32)
    return d


def train(edge_key: str = "distance", epochs: int = 25, batch: int = 16,
          lr: float = 5e-4, hidden: int = 64, blocks: int = 2, seed: int = 0,
          data: dict | None = None, verbose: bool = True,
          return_model: bool = False, stride: int = 6,
          dropout: float = 0.1, eval_every: int = 40,
          patience_evals: int = 12) -> dict:
    """Train the STGNN, checkpointing on validation at STEP granularity.

    Why steps and not epochs: at stride 6 an epoch is ~182 gradient steps, 4x what
    stride 24 gave. The first attempt kept per-epoch checkpointing and the optimum
    landed INSIDE epoch 0 for one config and epoch 2 for others, so the saved
    weights were already past it: neigh/self blew up to 2-4x initialisation and
    test MAE degraded to ~1.5. That was an optimisation-schedule failure, not
    evidence about the graph. Evaluating every `eval_every` steps puts the
    checkpoint where the minimum actually is.

    lr defaults to 5e-4 rather than 2e-3 for the same reason: 4x the steps per
    epoch at the old rate was overshooting.
    """
    torch.manual_seed(seed)
    np.random.seed(seed)
    d = build_tensors() if data is None else {k: v.copy() if isinstance(v, np.ndarray) else v
                                             for k, v in data.items()}
    T, N = d["T"], d["N"]
    wins = _windows(d["split"], T, stride)
    train_t = np.concatenate([np.arange(t0 + CONTEXT, t0 + WINDOW) for t0 in wins["train"]])
    d = _standardise(d, train_t)

    dev = device()
    A = adjacency(edge_key, d["uidx"], N).to(dev)
    y = torch.from_numpy(d["y"]).to(dev)
    msk = torch.from_numpy(d["mask"]).to(dev)
    x_nt = torch.from_numpy(d["x_nt"]).to(dev)
    x_g = torch.from_numpy(d["x_g"]).to(dev)
    x_s = torch.from_numpy(d["x_s"]).to(dev)

    model = STGNN(N, x_nt.shape[-1], x_g.shape[-1], x_s.shape[-1],
                  hidden=hidden, blocks=blocks, dropout=dropout).to(dev)
    n_par = sum(p.numel() for p in model.parameters())
    opt = torch.optim.AdamW(model.parameters(), lr=lr, weight_decay=1e-4)

    def evaluate(name):
        """Masked MAE/RMSE over one split's scored hours. Restores train mode."""
        model.eval()
        tot_ae = tot_se = tot_n = 0.0
        with torch.no_grad():
            idx = wins[name]
            for i in range(0, len(idx), batch):
                sl = torch.stack([torch.arange(t0, t0 + WINDOW)
                                  for t0 in idx[i:i + batch]]).to(dev)
                p = model(x_nt[sl], x_g[sl], x_s, A)[:, CONTEXT:]
                t, m = y[sl][:, CONTEXT:], msk[sl][:, CONTEXT:]
                tot_ae += ((p - t).abs() * m).sum().item()
                tot_se += (((p - t) ** 2) * m).sum().item()
                tot_n += m.sum().item()
        model.train()
        return tot_ae / tot_n, (tot_se / tot_n) ** 0.5

    best = {"val_mae": float("inf"), "step": 0, "state": None}
    hist, step, stop = [], 0, False
    for ep in range(epochs):
        idx = list(wins["train"])
        np.random.shuffle(idx)
        model.train()
        for i in range(0, len(idx), batch):
            sl = torch.stack([torch.arange(t0, t0 + WINDOW)
                              for t0 in idx[i:i + batch]]).to(dev)
            pred = model(x_nt[sl], x_g[sl], x_s, A)[:, CONTEXT:]
            t, m = y[sl][:, CONTEXT:], msk[sl][:, CONTEXT:]
            loss = (((pred - t) ** 2) * m).sum() / m.sum().clamp(min=1)
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            step += 1
            if step % eval_every == 0:
                vm, _ = evaluate("val")
                hist.append({"step": step, "val_mae": vm})
                if vm < best["val_mae"]:
                    best = {"val_mae": vm, "step": step,
                            "state": {k: v.detach().clone()
                                      for k, v in model.state_dict().items()}}
                elif (step - best["step"]) // eval_every >= patience_evals:
                    stop = True
                    break
                if verbose and step % (eval_every * 5) == 0:
                    print(f"    step {step:>5}  val MAE {vm:.4f}  (best {best['val_mae']:.4f} "
                          f"@ {best['step']})", flush=True)
        if stop:
            break

    if best["state"] is not None:
        model.load_state_dict(best["state"])
    te_mae, te_rmse = evaluate("test")

    model.eval()
    with torch.no_grad():
        idx, ys, ps = wins["test"], [], []
        for i in range(0, len(idx), batch):
            sl = torch.stack([torch.arange(t0, t0 + WINDOW)
                              for t0 in idx[i:i + batch]]).to(dev)
            pr = model(x_nt[sl], x_g[sl], x_s, A)[:, CONTEXT:]
            m = msk[sl][:, CONTEXT:]
            ys.append(y[sl][:, CONTEXT:][m].cpu()); ps.append(pr[m].cpu())
        yv, pv = torch.cat(ys), torch.cat(ps)
        r2 = 1 - ((yv - pv) ** 2).sum().item() / ((yv - yv.mean()) ** 2).sum().item()
        bias = (pv - yv).mean().item()

    neigh = float(sum(m.neigh_w.weight.norm().item() for m in model.gconv))
    slf = float(sum(m.self_w.weight.norm().item() for m in model.gconv))
    out = {"edges": edge_key, "params": n_par, "best_step": best["step"],
           "total_steps": step, "val_mae": best["val_mae"],
           "test_mae": te_mae, "test_rmse": te_rmse, "test_r2": r2, "test_bias": bias,
           "n_windows": {k: len(v) for k, v in wins.items()},
           "neigh_over_self": neigh / slf, "history": hist}
    if return_model:
        out["model"] = model; out["A"] = A
        out["tensors"] = {"x_nt": x_nt, "x_g": x_g, "x_s": x_s}
    return out


def predict_grid(model, tensors: dict, A: torch.Tensor, T: int, N: int,
                 batch: int = 8) -> np.ndarray:
    """Full [T, N] prediction grid, so Tier 2 effects use the same estimator as Tier 1.

    Windows are stepped by WINDOW-CONTEXT and only their SCORED tail is written,
    so every hour is predicted exactly once with a full convolution context behind
    it, with no double-writing, and no hour predicted from a cold start. The leading
    CONTEXT hours of the series have no such context and are left as NaN rather
    than filled with a worse estimate.
    """
    dev = next(model.parameters()).device
    out = np.full((T, N), np.nan, dtype=np.float32)
    step = WINDOW - CONTEXT
    starts = list(range(0, T - WINDOW + 1, step))
    model.eval()
    with torch.no_grad():
        for i in range(0, len(starts), batch):
            chunk = starts[i:i + batch]
            sl = torch.stack([torch.arange(t0, t0 + WINDOW) for t0 in chunk]).to(dev)
            p = model(tensors["x_nt"][sl], tensors["x_g"][sl], tensors["x_s"], A)
            tail = p[:, CONTEXT:].cpu().numpy()
            for j, t0 in enumerate(chunk):
                out[t0 + CONTEXT: t0 + WINDOW] = tail[j]
    return out
