#!/usr/bin/env python3
"""
Compute LLC-only energy/ED^2P bounds (SRAM vs JanS) and emit a compact summary,
writing BOTH CSVs into: results/<benchmark_name>/

Usage:
  python3 scripts/energy_ed2p.py <sram_simout> <jans_simout> [--overwrite]
  # Example:
  # python3 scripts/energy_ed2p.py results/520_omnetpp_r_sram_100M/sim.out results/520_omnetpp_r_JanS_cap_approx_100M/sim.out

Outputs in results/<benchmark_name>/ :
  - energy_bounds.csv
      config,time_s,l3_accesses,l3_misses,leak_W,dyn_lower_nJ,dyn_upper_nJ,
      energy_lower_J,energy_upper_J,ed2p_lower_J_s2,ed2p_upper_J_s2,energy_scope,notes

  - summary.csv
      timestamp_utc,benchmark,n_m,config,instructions,cycles,ipc,time_ns,
      l3_acc,l3_miss,l3_miss_rate_pct,dram_acc,dram_lat_value,dram_lat_unit,outdir

Notes:
  * Energy scope is LLC-only (nJ/event + W leakage).
  * DRAM latency is recorded as value + unit (no conversion).
"""

import argparse
import csv
import os
import re
from datetime import datetime
from typing import Tuple, Optional

# =========================
# Constants (LLC model)
# =========================
SRAM = dict(E_hit_nJ=0.565, E_miss_nJ=0.011, E_write_nJ=0.537, P_leak_W=3.438)
JANS = dict(E_hit_nJ=0.188, E_miss_nJ=0.077, E_write_nJ=2.305, P_leak_W=0.048)

# =========================
# Filesystem helpers
# =========================
def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def infer_bench_root_from_run_dir(run_dir: str) -> str:
    """
    From a run dir like:
      results/520_omnetpp_r_sram_100M     -> results/520_omnetpp_r
      results/520_omnetpp_r_JanS_cap_approx_100M -> results/520_omnetpp_r
      results/parsec_vips_simdev_n4_sram_100M -> results/parsec_vips_simdev_n4
    """
    results_root = os.path.dirname(run_dir)
    base = os.path.basename(run_dir)
    # strip _sram*, _JanS* (case-insensitive)
    base = re.sub(r'_(sram|jans)[^/]*', '', base, flags=re.IGNORECASE)
    # strip trailing _<N>M
    base = re.sub(r'_\d+M$', '', base)
    return os.path.join(results_root, f"output_{base}")

def extract_bench_name_and_nm(run_dir: str) -> Tuple[str, Optional[str]]:
    """
    From a run dir like:
      results/541_leela_r_sram_100M  -> ("541_leela_r", "100")
      results/520_omnetpp_r_JanS_cap_approx_100M -> ("520_omnetpp_r", "100")
    """
    leaf = os.path.basename(run_dir)
    # strip _sram*, _jans* (case-insensitive)
    bench = re.sub(r'_(sram|jans)[^/]*', '', leaf, flags=re.IGNORECASE)
    # capture trailing _<N>M
    m = re.search(r'_(\d+)M$', leaf)
    n_m = m.group(1) if m else None
    # strip the trailing _<N>M from bench name
    bench = re.sub(r'_\d+M$', '', bench)
    return bench, n_m

# =========================
# Parsing helpers
# =========================
def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception as e:
        print(f"[WARN] Cannot read {path}: {e}")
        return ""

def _first_num_after_pipe(t: str, label_regex: str) -> Optional[str]:
    m = re.search(label_regex + r'\s*\|\s*([0-9.]+)', t, flags=re.IGNORECASE | re.M)
    return m.group(1) if m else None

def parse_simout_full(path: str):
    """
    Parse a Sniper sim.out for a compact summary and energy inputs.
    Returns: dict with string values (numbers kept as strings) and raw text.
    """
    t = _read_text(path)
    out = dict()

    out["instructions"] = _first_num_after_pipe(t, r'^\s*Instructions')
    out["cycles"]       = _first_num_after_pipe(t, r'^\s*Cycles')
    out["ipc"]          = _first_num_after_pipe(t, r'^\s*IPC')
    out["time_ns"]      = _first_num_after_pipe(t, r'^\s*Time\s*\(ns\)')

    # L3 blocki
    blk = re.search(
        r'Cache\s+L3\s*\|(?P<body>.*?)(?=\n\s*DRAM\s+summary\s*\||\Z)',
        t, flags=re.IGNORECASE | re.S
    )
    if blk:
        body = blk.group('body')
        m_acc = re.search(r'num\s+cache\s+access(?:es)?\s*\|\s*([0-9,]+)', body, flags=re.IGNORECASE)
        m_mis = re.search(r'num\s+cache\s+miss(?:es)?\s*\|\s*([0-9,]+)',   body, flags=re.IGNORECASE)
        m_mr  = re.search(r'miss\s+rate\s*\|\s*([0-9.]+)\s*%',            body, flags=re.IGNORECASE)
        out["l3_acc"] = m_acc.group(1) if m_acc else None
        out["l3_miss"] = m_mis.group(1) if m_mis else None
        out["l3_miss_rate_pct"] = m_mr.group(1) if m_mr else None
    else:
        out["l3_acc"] = None
        out["l3_miss"] = None
        out["l3_miss_rate_pct"] = None
    # DRAM summary
    dblk = re.search(r'DRAM summary(.*?)(?:\n\s*\n|$)', t, flags=re.IGNORECASE | re.S | re.M)
    dram_acc = dram_lat_val = dram_lat_unit = None
    if dblk:
        dtxt = dblk.group(1)
        dacc = re.search(r'num\s+dram\s+(accesses|requests)\s*\|\s*([0-9]+)', dtxt, flags=re.IGNORECASE)
        dlat = re.search(r'average\s+dram\s+access\s+latency\s*\|\s*([0-9.]+)\s*([a-zA-Z]+)', dtxt, flags=re.IGNORECASE)
        if dacc: dram_acc = dacc.group(2)
        if dlat:
            dram_lat_val  = dlat.group(1)
            dram_lat_unit = dlat.group(2)

    out["dram_acc"] = dram_acc
    out["dram_lat_value"] = dram_lat_val
    out["dram_lat_unit"]  = dram_lat_unit
    return out, t

def to_float_or_nan(s: Optional[str]) -> float:
    try:
        return float(s)
    except Exception:
        return float('nan')

def to_int_or_zero(s: Optional[str]) -> int:
    try:
        return int(str(s).replace(',', '')) if s not in (None, '') else 0
    except Exception:
        return 0
# =========================
# Energy math
# =========================
def energy_bounds(E_hit_nJ: float, E_miss_nJ: float, E_write_nJ: float,
                  P_leak_W: float, T_s: float, acc: int, mis: int):
    hits = max(acc - mis, 0)
    dyn_lo_nJ = E_hit_nJ * hits + E_miss_nJ * mis   # assume all hits are reads
    dyn_hi_nJ = E_write_nJ * hits + E_miss_nJ * mis # assume all hits are writes
    eleak_J   = P_leak_W * (T_s if T_s == T_s else 0.0)
    elo_J     = dyn_lo_nJ * 1e-9 + eleak_J
    ehi_J     = dyn_hi_nJ * 1e-9 + eleak_J
    return (min(elo_J, ehi_J), max(elo_J, ehi_J)), (dyn_lo_nJ, dyn_hi_nJ), eleak_J

def ed2p(E_J: float, T_s: float) -> float:
    if not (E_J == E_J) or not (T_s == T_s):  # NaN guard
        return float('nan')
    return E_J * (T_s ** 2)

# =========================
# CSV writers
# =========================
def write_csv_rows(path: str, header: list, rows: list, overwrite: bool):
    if overwrite and os.path.exists(path):
        os.remove(path)
    write_hdr = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if write_hdr:
            w.writerow(header)
        w.writerows(rows)

# =========================
# Main
# =========================
def main():
    ap = argparse.ArgumentParser(description="Create energy_bounds.csv and summary.csv in results/<benchmark_name>/")
    ap.add_argument("sram_simout", help="Path to SRAM sim.out")
    ap.add_argument("jans_simout", help="Path to JanS sim.out")
    ap.add_argument("--overwrite", action="store_true", help="Truncate existing CSVs before writing")
    args = ap.parse_args()

    sram_simout = os.path.abspath(args.sram_simout)
    jans_simout = os.path.abspath(args.jans_simout)

    sram_dir = os.path.dirname(sram_simout)
    jans_dir = os.path.dirname(jans_simout)

    # Output directory under results/<bench>/
    out_dir = os.path.dirname(sram_dir)
    ensure_dir(out_dir)

    # Infer name + ROI for summary rows
    bench_name, n_m = extract_bench_name_and_nm(sram_dir)

    # Parse both simouts (strings in dict)
    sram_parsed, _ = parse_simout_full(sram_simout)
    jans_parsed, _ = parse_simout_full(jans_simout)

    # Convert for energy math
    T_s = to_float_or_nan(sram_parsed.get("time_ns")) / 1e9 if sram_parsed.get("time_ns") else float('nan')
    T_n = to_float_or_nan(jans_parsed.get("time_ns")) / 1e9 if jans_parsed.get("time_ns") else float('nan')
    A_s = to_int_or_zero(sram_parsed.get("l3_acc"))
    M_s = to_int_or_zero(sram_parsed.get("l3_miss"))
    A_n = to_int_or_zero(jans_parsed.get("l3_acc"))
    M_n = to_int_or_zero(jans_parsed.get("l3_miss"))

    # Energy bounds
    (s_lo, s_hi), (sd_lo, sd_hi), s_leak = energy_bounds(**SRAM, T_s=T_s, acc=A_s, mis=M_s)
    (n_lo, n_hi), (nd_lo, nd_hi), n_leak = energy_bounds(**JANS, T_s=T_n, acc=A_n, mis=M_n)

    # ===== energy_bounds.csv =====
    energy_path = os.path.join(out_dir, "energy_bounds.csv")
    energy_header = [
        "config","time_s","l3_accesses","l3_misses","leak_W",
        "dyn_lower_nJ","dyn_upper_nJ",
        "energy_lower_J","energy_upper_J",
        "ed2p_lower_J_s2","ed2p_upper_J_s2",
        "energy_scope","notes"
    ]
    energy_rows = [
        ["SRAM",
         f"{T_s:.6f}", str(A_s), str(M_s), f"{SRAM['P_leak_W']:.3f}",
         f"{sd_lo:.0f}", f"{sd_hi:.0f}",
         f"{s_lo:.6f}", f"{s_hi:.6f}",
         f"{ed2p(s_lo,T_s):.9e}", f"{ed2p(s_hi,T_s):.9e}",
         "llc_only", ""],
        ["JanS",
         f"{T_n:.6f}", str(A_n), str(M_n), f"{JANS['P_leak_W']:.3f}",
         f"{nd_lo:.0f}", f"{nd_hi:.0f}",
         f"{n_lo:.6f}", f"{n_hi:.6f}",
         f"{ed2p(n_lo,T_n):.9e}", f"{ed2p(n_hi,T_n):.9e}",
         "llc_only", ""],
    ]
    write_csv_rows(energy_path, energy_header, energy_rows, overwrite=args.overwrite)
    print(f"[OK] wrote {energy_path}")

    # ===== summary.csv =====
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    summary_path = os.path.join(out_dir, "summary.csv")
    summary_header = [
        "timestamp_utc","benchmark","n_m","config",
        "instructions","cycles","ipc","time_ns",
        "l3_acc","l3_miss","l3_miss_rate_pct",
        "dram_acc","dram_lat_value","dram_lat_unit","outdir"
    ]
    sram_row = [
        ts, bench_name, n_m or "", "SRAM",
        sram_parsed.get("instructions") or "",
        sram_parsed.get("cycles") or "",
        sram_parsed.get("ipc") or "",
        sram_parsed.get("time_ns") or "",
        sram_parsed.get("l3_acc") or "",
        sram_parsed.get("l3_miss") or "",
        sram_parsed.get("l3_miss_rate_pct") or "",
        sram_parsed.get("dram_acc") or "",
        sram_parsed.get("dram_lat_value") or "",
        sram_parsed.get("dram_lat_unit") or "",
        sram_dir
    ]
    jans_row = [
        ts, bench_name, n_m or "", "JanS",
        jans_parsed.get("instructions") or "",
        jans_parsed.get("cycles") or "",
        jans_parsed.get("ipc") or "",
        jans_parsed.get("time_ns") or "",
        jans_parsed.get("l3_acc") or "",
        jans_parsed.get("l3_miss") or "",
        jans_parsed.get("l3_miss_rate_pct") or "",
        jans_parsed.get("dram_acc") or "",
        jans_parsed.get("dram_lat_value") or "",
        jans_parsed.get("dram_lat_unit") or "",
        jans_dir
    ]
    write_csv_rows(summary_path, summary_header, [sram_row, jans_row], overwrite=args.overwrite)
    print(f"[OK] wrote {summary_path}")

    # ===== console summary =====
    print("\n==== Post-run energy/ED^2P (LLC-only, bounds) ====")
    print(f"SRAM: time={T_s:.6f}s, L3_acc={A_s}, L3_miss={M_s}  -> E={s_lo:.6f}..{s_hi:.6f} J  (leak={s_leak:.6f} J)")
    print(f"JanS: time={T_n:.6f}s, L3_acc={A_n}, L3_miss={M_n}  -> E={n_lo:.6f}..{n_hi:.6f} J  (leak={n_leak:.6f} J)")

if __name__ == "__main__":
    main()
