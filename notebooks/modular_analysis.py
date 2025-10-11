import os
import re
import warnings
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.ticker import EngFormatter, MaxNLocator

SNIPER_RESULTS_DIR = "/home/skataoka26/COSC_498/miniMXE/results/sniper_llc_32mb_16w_20251002T020050Z"
DYNAMORIO_LOGS_DIR = "/home/skataoka26/COSC_498/miniMXE/results_trace/logs"
TIMESTAMP_PREFIX = "20250929T203551Z"

# Global cache for parsed data
_cache = {}

def safe_div(numer, denom):
    numer = np.array(numer, dtype=float)
    denom = np.array(denom, dtype=float)
    with np.errstate(divide='ignore', invalid='ignore'):
        out = np.where((denom > 0) & np.isfinite(denom), numer / denom, np.nan)
    return out

def geomean(values):
    vals = np.array([v for v in np.ravel(values) if v > 0 and np.isfinite(v)], dtype=float)
    return float(np.exp(np.mean(np.log(vals)))) if len(vals) else np.nan

def parse_sniper(base_dir):
    energy_rows = []
    summary_rows = []
    print(f"[parse_sniper] Searching under: {base_dir}")
    for root, _, files in os.walk(base_dir):
        if os.path.basename(root).startswith("output_") and "summary.csv" in files and "energy_bounds.csv" in files:
            energy_path = os.path.join(root, "energy_bounds.csv")
            summary_path = os.path.join(root, "summary.csv")
            try:
                e = pd.read_csv(energy_path)
                s = pd.read_csv(summary_path)
            except Exception:
                continue
            e_keep = ["benchmark", "config", "time_s", "energy_exact_J", "leak_J", "dyn_exact_nJ"]
            e = e[[c for c in e_keep if c in e.columns]].copy()
            s_base = ["benchmark", "config", "ipc", "time_ns", "l3_miss_rate_pct"]
            keep_s = [c for c in s_base if c in s.columns]
            for col in s.columns:
                cl = col.lower()
                if ("l3" in cl or "llc" in cl) and (
                    "access" in cl or "read" in cl or "write" in cl or "hit" in cl or "miss" in cl or "evict" in cl or "wb" in cl
                ):
                    if col not in keep_s:
                        keep_s.append(col)
            s = s[keep_s].copy()
            for col in ["time_s", "energy_exact_J", "leak_J", "dyn_exact_nJ"]:
                if col in e.columns:
                    e[col] = pd.to_numeric(e[col], errors="coerce")
            for col in s.columns:
                if col not in ("benchmark", "config"):
                    s[col] = pd.to_numeric(s[col], errors="coerce")
            if "dyn_exact_nJ" in e.columns:
                e["dyn_exact_J"] = e["dyn_exact_nJ"] * 1e-9
            energy_rows.append(e)
            summary_rows.append(s)
    if not energy_rows or not summary_rows:
        raise FileNotFoundError(f"Could not find summary.csv and energy_bounds.csv under: {base_dir}")
    E = pd.concat(energy_rows, ignore_index=True)
    S = pd.concat(summary_rows, ignore_index=True)
    df = pd.merge(E, S, on=["benchmark", "config"], how="inner")
    df = df.dropna(subset=["benchmark", "config", "time_s", "energy_exact_J"])
    print(f"[parse_sniper] Merged DataFrame shape: {df.shape}")
    return df

def _kv_from_line(s):
    kv = {}
    m = re.search(r"\bscope=(\w+)", s)
    if m:
        kv["scope"] = m.group(1)
    for k, v in re.findall(r"([A-Za-z0-9_]+)=([+\-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?|nan)", s):
        try:
            kv[k] = float("nan") if v.lower() == "nan" else float(v)
        except Exception:
            pass
    return kv

def parse_dynamorio(logs_dir, ts_prefix):
    rows = []
    print(f"[parse_dynamorio] Searching logs: {logs_dir} (prefix={ts_prefix})")
    if not os.path.isdir(logs_dir):
        print("[parse_dynamorio] Logs dir not found; returning empty DataFrame.")
        return pd.DataFrame()
    r_weight_keys = {"read_entropy", "read_local_entropy", "read_footprint90L"}
    w_weight_keys = {"write_entropy", "write_local_entropy", "write_footprint90L"}
    cumulative_last_keys = [
        "reads", "writes", "bytes_read", "bytes_written",
        "global_footprint_bytes", "read_unique_lines", "write_unique_lines",
    ]
    max_keys = ["read_unique", "write_unique", "uniq_lines", "uniq_pages", "footprint_bytes"]
    found = 0
    for fname in os.listdir(logs_dir):
        if not (fname.startswith(ts_prefix) and fname.endswith("_instr.rwstats.log")):
            continue
        m = re.match(rf"{re.escape(ts_prefix)}_(.+?)_instr\.rwstats\.log$", fname)
        if not m:
            continue
        found += 1
        benchmark = m.group(1)
        fpath = os.path.join(logs_dir, fname)
        ivals = []
        try:
            with open(fpath, "r", errors="ignore") as f:
                for line in f:
                    if "scope=" not in line:
                        continue
                    kv = _kv_from_line(line.strip())
                    if not kv:
                        continue
                    sc = str(kv.get("scope", "")).lower()
                    if sc in ("interval", "final"):
                        ivals.append(kv)
        except Exception:
            continue
        if not ivals:
            continue
        reads_seq = [r.get("reads", np.nan) for r in ivals]
        writes_seq = [r.get("writes", np.nan) for r in ivals]
        dreads = [np.nan]
        dwrites = [np.nan]
        for i in range(1, len(ivals)):
            a, b = reads_seq[i-1], reads_seq[i]
            c, d = writes_seq[i-1], writes_seq[i]
            dreads.append((b - a) if (np.isfinite(a) and np.isfinite(b) and b >= a) else np.nan)
            dwrites.append((d - c) if (np.isfinite(c) and np.isfinite(d) and d >= c) else np.nan)
        r_wts, w_wts = [], []
        for i, r in enumerate(ivals):
            rtot = r.get("read_total", np.nan)
            wtot = r.get("write_total", np.nan)
            r_wts.append(rtot if np.isfinite(rtot) else dreads[i])
            w_wts.append(wtot if np.isfinite(wtot) else dwrites[i])
        sum_read_total = float(np.nansum([w for w in r_wts if np.isfinite(w)])) if any(np.isfinite(w) for w in r_wts) else np.nan
        sum_write_total = float(np.nansum([w for w in w_wts if np.isfinite(w)])) if any(np.isfinite(w) for w in w_wts) else np.nan
        agg = {"benchmark": benchmark, "scope": "aggregate"}
        agg["read_total"] = sum_read_total
        agg["write_total"] = sum_write_total
        def _wavg(keys, weights):
            den = float(np.nansum([w for w in weights if np.isfinite(w)]))
            for k in keys:
                num = 0.0
                if den > 0:
                    for i, row in enumerate(ivals):
                        v = row.get(k, np.nan)
                        w = weights[i]
                        if np.isfinite(v) and np.isfinite(w) and w > 0:
                            num += v * w
                    agg[k] = num / den if den > 0 else np.nan
                else:
                    agg[k] = np.nan
        _wavg(r_weight_keys, r_wts)
        _wavg(w_weight_keys, w_wts)
        for k in cumulative_last_keys:
            for row in reversed(ivals):
                v = row.get(k, np.nan)
                if np.isfinite(v):
                    agg[k] = v
                    break
        if np.isfinite(agg.get("read_unique_lines", np.nan)):
            agg["read_unique"] = agg["read_unique_lines"]
        else:
            rmax = np.nan
            for row in ivals:
                v = row.get("read_unique", np.nan)
                if np.isfinite(v):
                    rmax = v if not np.isfinite(rmax) else max(rmax, v)
            if np.isfinite(rmax):
                agg["read_unique"] = rmax
        if np.isfinite(agg.get("write_unique_lines", np.nan)):
            agg["write_unique"] = agg["write_unique_lines"]
        else:
            wmax = np.nan
            for row in ivals:
                v = row.get("write_unique", np.nan)
                if np.isfinite(v):
                    wmax = v if not np.isfinite(wmax) else max(wmax, v)
            if np.isfinite(wmax):
                agg["write_unique"] = wmax
        for k in max_keys:
            vmax = np.nan
            for row in ivals:
                v = row.get(k, np.nan)
                if np.isfinite(v):
                    vmax = v if not np.isfinite(vmax) else max(vmax, v)
            if np.isfinite(vmax):
                agg[k] = vmax
        for k in ("instrs", "instructions"):
            s = float(np.nansum([row.get(k, np.nan) for row in ivals if np.isfinite(row.get(k, np.nan))]))
            lastv = np.nan
            for row in reversed(ivals):
                v = row.get(k, np.nan)
                if np.isfinite(v):
                    lastv = v
                    break
            if np.isfinite(s) or np.isfinite(lastv):
                agg[k] = max(s if np.isfinite(s) else -np.inf, lastv if np.isfinite(lastv) else -np.inf)
        rows.append(agg)
    print(f"[parse_dynamorio] Processed {found} files; rows={len(rows)}")
    return pd.DataFrame(rows)

def process_dynamorio_data(logs_dir, ts_prefix):
    dr = parse_dynamorio(logs_dir, ts_prefix)
    if dr.empty:
        warnings.warn("DynamoRIO data not found or empty.")
        return pd.DataFrame()
    feat_map = {
        "rtotal": "read_total",
        "runique": "read_unique",
        "90%f_tr": "read_footprint90L",
        "Hrg": "read_entropy",
        "Hrl": "read_local_entropy",
        "wtotal": "write_total",
        "wunique": "write_unique",
        "90%f_tw": "write_footprint90L",
        "Hwg": "write_entropy",
        "Hwl": "write_local_entropy",
    }
    feats = pd.DataFrame({"benchmark": dr["benchmark"]})
    for out_name, src in feat_map.items():
        feats[out_name] = pd.to_numeric(dr.get(src), errors="coerce")
    feats = feats.set_index("benchmark")
    feats['read_intensity'] = safe_div(feats.get('runique'), feats.get('rtotal'))
    feats['write_intensity'] = safe_div(feats.get('wunique'), feats.get('wtotal'))
    feats['rw_ratio_total'] = safe_div(feats.get('rtotal'), feats.get('wtotal'))
    feats['rw_ratio_unique'] = safe_div(feats.get('runique'), feats.get('wunique'))
    feats['rw_ratio_global_entropy'] = safe_div(feats.get('Hrg'), feats.get('Hwg'))
    feats['rw_ratio_local_entropy'] = safe_div(feats.get('Hrl'), feats.get('Hwl'))
    feats['rw_ratio_90_footprint'] = safe_div(feats.get('90%f_tr'), feats.get('90%f_tw'))
    return feats

def corr_heatmap(ax, matrix, row_labels, col_labels, title):
    im = ax.imshow(matrix, aspect="auto", vmin=-1, vmax=1, cmap="coolwarm")
    ax.set_yticks(np.arange(len(row_labels)))
    ax.set_yticklabels(row_labels)
    ax.set_xticks(np.arange(len(col_labels)))
    ax.set_xticklabels(col_labels, rotation=45, ha="right")
    ax.set_title(title, fontsize=11)
    for i in range(matrix.shape[0]):
        for j in range(matrix.shape[1]):
            val = matrix[i, j]
            if np.isfinite(val):
                ax.text(j, i, f"{val:.2f}", ha="center", va="center",
                        color="white" if abs(val) > 0.6 else "black", fontsize=8)
    return im

def _resolve_sniper_dir(runid):
    if runid:
        if os.path.isabs(runid):
            candidate = runid
        else:
            base = os.path.dirname(SNIPER_RESULTS_DIR)
            candidate = os.path.join(base, runid)
        for root, _, files in os.walk(candidate):
            if os.path.basename(root).startswith("output_") and \
               "summary.csv" in files and "energy_bounds.csv" in files:
                return candidate
        print(f"[WARNING] runid '{runid}' not valid; falling back to default.")
    return SNIPER_RESULTS_DIR

def get_config(runid=None, ts_prefix=None, logs_dir=None):
    cfg = {}
    cfg['sniper_dir'] = _resolve_sniper_dir(runid)
    cfg['logs_dir'] = logs_dir if logs_dir else DYNAMORIO_LOGS_DIR
    cfg['ts_prefix'] = ts_prefix if ts_prefix else TIMESTAMP_PREFIX
    return cfg

def load_data(runid=None, force_reload=False):
    if not force_reload and 'sniper' in _cache and 'feats' in _cache:
        return _cache['sniper'], _cache['feats']
    cfg = get_config(runid)
    sniper = parse_sniper(cfg['sniper_dir'])
    feats = process_dynamorio_data(cfg['logs_dir'], cfg['ts_prefix'])
    _cache['sniper'] = sniper
    _cache['feats'] = feats
    return sniper, feats

def get_processed(runid=None):
    sniper, feats = load_data(runid)
    sram = sniper[sniper["config"] == "SRAM"].set_index("benchmark")
    jans = sniper[sniper["config"] == "JanS"].set_index("benchmark")
    common = sram.index.intersection(jans.index)
    if len(common) < 2:
        raise RuntimeError("Need at least 2 common benchmarks.")
    order = sram.loc[common].sort_values("time_s").index.tolist()
    sram = sram.loc[order]
    jans = jans.loc[order]
    targets_abs = jans[["dyn_exact_J", "leak_J", "energy_exact_J", "time_s"]].copy()
    targets_abs = targets_abs.rename(columns={
        "dyn_exact_J": "Dyn_energy",
        "leak_J": "Leakage",
        "energy_exact_J": "LLC_energy",
        "time_s": "exe_time",
    })
    ratio_targets = pd.DataFrame(index=order)
    ratio_targets["dyn_energy_ratio"] = pd.Series(safe_div(jans["dyn_exact_J"], sram["dyn_exact_J"]), index=order)
    ratio_targets["energy_ratio"] = pd.Series(safe_div(jans["energy_exact_J"], sram["energy_exact_J"]), index=order)
    ratio_targets["exe_time_ratio"] = pd.Series(safe_div(jans["time_s"], sram["time_s"]), index=order)
    corr_df = feats.join(targets_abs, how="inner").join(ratio_targets, how="inner")
    corr_df = corr_df.loc[corr_df.index.intersection(order)]
    corr_df = corr_df.reindex(order)
    return sram, jans, order, targets_abs, ratio_targets, corr_df, feats

def plot_corr_heatmap(runid=None, out_dir=None):
    _, _, order, _, _, corr_df, _ = get_processed(runid)
    candidate_features = [
        "rtotal","runique","90%f_tr","Hrg","Hrl",
        "wtotal","wunique","90%f_tw","Hwg","Hwl",
        "read_intensity","write_intensity",
        "rw_ratio_total","rw_ratio_unique",
        "rw_ratio_global_entropy","rw_ratio_local_entropy","rw_ratio_90_footprint"
    ]
    feature_names = [f for f in candidate_features if f in corr_df.columns and not corr_df[f].isna().all()]
    target_candidates = ["Dyn_energy", "Leakage", "LLC_energy", "exe_time",
                         "dyn_energy_ratio", "energy_ratio", "exe_time_ratio"]
    target_cols = [c for c in target_candidates if c in corr_df.columns and not corr_df[c].isna().all()]
    corr_mat = corr_df[feature_names + target_cols].corr(method="pearson").loc[feature_names, target_cols]
    fig, ax = plt.subplots(1, 1, figsize=(10, max(4, 0.30 * len(feature_names))))
    im = corr_heatmap(ax, corr_mat.values, feature_names, target_cols,
                      "Features vs JanS (absolute + ratios) — Pearson's r")
    cbar = fig.colorbar(im, ax=ax, fraction=0.045, pad=0.02)
    cbar.set_label("pearson")
    fig.suptitle("Correlation: Memory Features ↔ JanS (Pearson)", y=0.995, fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    if out_dir:
        fig.savefig(os.path.join(out_dir, "corr_heatmap_pearson.png"), dpi=200, bbox_inches="tight")
        corr_mat.to_csv(os.path.join(out_dir, "corr_matrix_pearson.csv"))
    return fig, corr_mat

def plot_grouped_bars_norm(runid=None, out_dir=None, n_benchmarks=12):
    _, _, order, _, _, _, feats = get_processed(runid)
    feature_cols = ["rtotal","wtotal","runique","wunique","90%f_tr","90%f_tw",
                    "Hrg","Hrl","Hwg","Hwl","read_intensity","write_intensity"]
    present = [c for c in feature_cols if c in feats.columns and not feats[c].isna().all()]
    top = order[:n_benchmarks] if len(order) >= n_benchmarks else order
    bms = [b for b in top if b in feats.index]
    df = feats.loc[bms, present].astype(float).copy()
    for c in df.columns:
        arr = df[c].values
        p95 = np.nanpercentile(arr, 95) if np.isfinite(np.nanmax(arr)) else np.nan
        denom = p95 if (np.isfinite(p95) and p95 > 0) else (np.nanmax(arr) if np.isfinite(np.nanmax(arr)) and np.nanmax(arr) > 0 else 1.0)
        df[c] = df[c] / denom
    df = df.clip(lower=0, upper=1.25)
    n_bm, n_feat = len(df.index), len(df.columns)
    x = np.arange(n_bm)
    width = min(0.80 / max(n_feat, 1), 0.18)
    fig_w = max(10, 0.6 * n_bm + 4)
    fig, ax = plt.subplots(1, 1, figsize=(fig_w, 5.2))
    for i, c in enumerate(df.columns):
        offsets = (i - (n_feat - 1) / 2.0) * width
        y = np.nan_to_num(df[c].values, nan=0.0)
        ax.bar(x + offsets, y, width=width, label=c)
    ax.set_xticks(x)
    ax.set_xticklabels(df.index.tolist(), rotation=45, ha="right")
    ax.set_ylabel("value / P95 (per feature)")
    ax.set_title(f"DynamoRIO features — P95-normalised ({len(bms)} benchmarks)")
    ax.grid(axis="y", alpha=0.25)
    ax.set_axisbelow(True)
    if n_feat > 6:
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), title="feature", frameon=False)
        fig.tight_layout(rect=[0, 0, 0.85, 1])
    else:
        ax.legend(ncol=min(n_feat, 6), frameon=False)
        fig.tight_layout()
    if out_dir:
        fig.savefig(os.path.join(out_dir, "features_grouped_bar_normalised_p95.png"), dpi=200, bbox_inches="tight")
        df.to_csv(os.path.join(out_dir, "features_grouped_bar_normalised_p95_values.csv"))
    return fig, df

def plot_grouped_bars_raw(runid=None, out_dir=None, n_benchmarks=12):
    _, _, order, _, _, _, feats = get_processed(runid)
    feature_cols = ["rtotal","wtotal","runique","wunique","90%f_tr","90%f_tw",
                    "Hrg","Hrl","Hwg","Hwl","read_intensity","write_intensity"]
    present = [c for c in feature_cols if c in feats.columns and not feats[c].isna().all()]
    top = order[:n_benchmarks] if len(order) >= n_benchmarks else order
    bms = [b for b in top if b in feats.index]
    df = feats.loc[bms, present].astype(float).copy()
    n_bm, n_feat = len(df.index), len(df.columns)
    x = np.arange(n_bm)
    width = min(0.80 / max(n_feat, 1), 0.18)
    fig_w = max(10, 0.6 * n_bm + 4)
    fig, ax = plt.subplots(1, 1, figsize=(fig_w, 5.2))
    for i, c in enumerate(df.columns):
        offsets = (i - (n_feat - 1) / 2.0) * width
        y = np.nan_to_num(df[c].values, nan=0.0)
        ax.bar(x + offsets, y, width=width, label=c)
    ax.set_xticks(x)
    ax.set_xticklabels(df.index.tolist(), rotation=45, ha="right")
    ax.set_ylabel("value")
    ax.set_title(f"DynamoRIO features — RAW (log y) ({len(bms)} benchmarks)")
    ax.grid(axis="y", alpha=0.25)
    ax.set_axisbelow(True)
    pos_vals = np.concatenate([np.nan_to_num(df[c].values, nan=np.nan) for c in df.columns])
    pos_vals = pos_vals[np.isfinite(pos_vals) & (pos_vals > 0)]
    if pos_vals.size:
        ax.set_yscale("log")
        ax.set_ylim(bottom=max(pos_vals.min() * 0.5, 1e-12))
    if n_feat > 6:
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), title="feature", frameon=False)
        fig.tight_layout(rect=[0, 0, 0.85, 1])
    else:
        ax.legend(ncol=min(n_feat, 6), frameon=False)
        fig.tight_layout()
    if out_dir:
        fig.savefig(os.path.join(out_dir, "features_grouped_bar_raw_logy.png"), dpi=200, bbox_inches="tight")
        df.to_csv(os.path.join(out_dir, "features_grouped_bar_raw_values.csv"))
    return fig, df

def plot_feature_corr_pair(runid=None, out_dir=None):
    _, _, _, _, _, _, feats = get_processed(runid)
    feature_cols = ["rtotal","wtotal","runique","wunique","90%f_tr","90%f_tw",
                    "Hrg","Hrl","Hwg","Hwl","read_intensity","write_intensity"]
    cols = [c for c in feature_cols if c in feats.columns and not feats[c].isna().all()]
    M = feats[cols].astype(float)
    cS = M.corr(method="spearman")
    cP = M.corr(method="pearson")
    size = max(6, 0.5 * len(cols) + 4)
    fig, axes = plt.subplots(1, 2, figsize=(2 * size + 2, size))
    im0 = corr_heatmap(axes[0], cS.values, cols, cols, "Spearman")
    im1 = corr_heatmap(axes[1], cP.values, cols, cols, "Pearson")
    cbar = fig.colorbar(im1, ax=axes.ravel().tolist(), fraction=0.035, pad=0.02)
    cbar.set_label("correlation")
    fig.suptitle("Feature ↔ Feature Correlation (Spearman & Pearson)", y=0.995, fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    if out_dir:
        fig.savefig(os.path.join(out_dir, "features_vs_features_corr_pair.png"), dpi=200, bbox_inches="tight")
        cS.to_csv(os.path.join(out_dir, "features_vs_features_corr_spearman.csv"))
        cP.to_csv(os.path.join(out_dir, "features_vs_features_corr_pearson.csv"))
    return fig, cS, cP

def plot_feature_facets(runid=None, out_dir=None, n_benchmarks=12, ncols=4, nrows=3):
    _, _, order, _, _, _, feats = get_processed(runid)
    feature_cols = ["rtotal", "wtotal", "runique", "wunique",
                   "read_intensity", "write_intensity",
                   "90%f_tr", "90%f_tw",
                   "Hrg", "Hrl", "Hwg", "Hwl"]
    present = [c for c in feature_cols if c in feats.columns and not feats[c].isna().all()]
    top = order[:n_benchmarks] if len(order) >= n_benchmarks else order
    bms = [b for b in top if b in feats.index]
    label_formatter = EngFormatter(places=3, sep="")
    axis_formatter = EngFormatter(places=3)
    w_per = 2.8 + 2.8 + 0.05 * len(bms)
    h_per = 2.6 + 2.6
    fig, axes = plt.subplots(nrows, ncols, figsize=(ncols * w_per, nrows * h_per))
    axes = np.ravel(axes)
    x = np.arange(len(bms))
    total_slots = nrows * ncols
    y_margin = 0.06
    for i in range(total_slots):
        ax = axes[i]
        if i >= len(present):
            ax.axis("off")
            continue
        c = present[i]
        y = feats.loc[bms, c].astype(float).values
        finite_y = y[np.isfinite(y)]
        lo = float(np.min(finite_y)) if finite_y.size else 0.0
        hi = float(np.max(finite_y)) if finite_y.size else 1.0
        span = max(hi - lo, 1e-12)
        ax.set_ylim(lo - y_margin * span, hi + y_margin * span)
        ax.bar(x, np.nan_to_num(y, nan=0.0), width=0.8)
        ax.set_title(c, fontsize=9)
        ax.set_xticks(x)
        ax.set_xticklabels(bms, rotation=45, ha="right", fontsize=7)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=4, prune='both'))
        ax.yaxis.set_major_formatter(axis_formatter)
        ax.tick_params(axis="y", labelsize=7)
        ax.grid(axis="y", alpha=0.25)
        ax.set_axisbelow(True)
        y_lo, y_hi = ax.get_ylim()
        offset = (y_hi - y_lo) * 0.02
        max_label_y = -np.inf
        for xi, yi in enumerate(y):
            if not np.isfinite(yi):
                continue
            label_y = yi + offset
            max_label_y = max(max_label_y, label_y)
            ax.text(xi, label_y, label_formatter(yi), ha="center", va="bottom", fontsize=7)
        if np.isfinite(max_label_y) and max_label_y > y_hi:
            ax.set_ylim(y_lo, max_label_y * 1.04)
    fig.suptitle(f"Per-feature bars across benchmarks (raw units) — {len(bms)} benchmarks", y=0.995, fontsize=12)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    if out_dir:
        fig.savefig(os.path.join(out_dir, "feature_facets_raw_3x4.png"), dpi=200, bbox_inches="tight")
    return fig, axes

def save_csvs(runid=None, out_dir=None):
    if not out_dir:
        out_dir = os.getcwd()
    sniper, feats = load_data(runid)
    _, _, _, _, _, corr_df, _ = get_processed(runid)
    sniper.to_csv(os.path.join(out_dir, "sniper_raw.csv"), index=False)
    feats.to_csv(os.path.join(out_dir, "dynamorio_raw.csv"))
    corr_df.to_csv(os.path.join(out_dir, "corr_input_joined.csv"))
    print(f"CSVs saved to {out_dir}")

def run_dynamorio():
    dr = parse_dynamorio(DYNAMORIO_LOGS_DIR, TIMESTAMP_PREFIX)
    feats = process_dynamorio_data(DYNAMORIO_LOGS_DIR, TIMESTAMP_PREFIX)
    return dr, feats