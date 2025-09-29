#!/usr/bin/env python3
"""
Compute LLC-only energy/ED^2P for SRAM vs JanS (or any "tech" run in the JanS slot).

What this script does:
- Reads timing + coarse L3 stats from each run's sim.out
- Reads exact L3 hit/miss breakdown from sim.stats.sqlite3
- NEW: Reads LLC energy constants (pJ/mW) from each run's sim.cfg if present
- Writes CSVs into: <OUT_ROOT>/output_<bench>/
    * energy_bounds.csv   (includes exact energy + bounds)
    * summary.csv         (text + sqlite stats side-by-side + avg L3 hit ns)

Usage:
  python3 scripts/energy_ed2p_v3.py <sram_simout> <jans_simout>

Notes:
  * Energy scope is LLC-only (nJ/event + W leakage).
  * If a run's sim.cfg includes:
       perf_model/l3_cache/llc/e_read_hit_pJ
       perf_model/l3_cache/llc/e_write_hit_pJ
       perf_model/l3_cache/llc/e_miss_pJ
       perf_model/l3_cache/llc/p_leak_mW
     those are parsed and used (converted to nJ/W). Otherwise we fall back to defaults.
"""

import argparse
import csv
import os
import re
import sqlite3
from datetime import datetime, timezone

# =========================
# Defaults (fallback if sim.cfg has no overrides)
# =========================
SRAM_DEFAULT = dict(E_hit_nJ=0.565, E_miss_nJ=0.011, E_write_nJ=0.537, P_leak_W=3.438)
JANS_DEFAULT = dict(E_hit_nJ=0.188, E_miss_nJ=0.077, E_write_nJ=2.305, P_leak_W=0.048)

# =========================
# Filesystem helpers
# =========================
def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

def infer_bench_root_from_run_dir(run_dir):
    """
    results/541_leela_r_sram_100M -> results/output_541_leela_r
    results/541_leela_r_JanS_cap_approx_100M -> results/output_541_leela_r
    """
    results_root = os.path.dirname(run_dir)
    base = os.path.basename(run_dir)
    base = re.sub(r'_(sram|jans)[^/]*', '', base, flags=re.IGNORECASE)
    base = re.sub(r'_\d+M$', '', base)
    return os.path.join(results_root, f"output_{base}")

def extract_bench_name_and_nm(run_dir):
    leaf = os.path.basename(run_dir)
    bench = re.sub(r'_(sram|jans)[^/]*', '', leaf, flags=re.IGNORECASE)
    m = re.search(r'_(\d+)M$', leaf)
    n_m = m.group(1) if m else None
    bench = re.sub(r'_\d+M$', '', bench)
    return bench, n_m

# =========================
# Parsing helpers (sim.out)
# =========================
def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except Exception as e:
        print(f"[WARN] Cannot read {path}: {e}")
        return ""

def _first_num_after_pipe(t, label_regex):
    m = re.search(label_regex + r'\s*\|\s*([0-9.]+)', t, flags=re.IGNORECASE | re.M)
    return m.group(1) if m else None

def parse_simout_full(path):
    """
    Parse a Sniper sim.out for compact summary fields.
    Returns: (dict[str,str], raw_text)
    """
    t = _read_text(path)
    out = dict()

    out["instructions"] = _first_num_after_pipe(t, r'^\s*Instructions')
    out["cycles"]       = _first_num_after_pipe(t, r'^\s*Cycles')
    out["ipc"]          = _first_num_after_pipe(t, r'^\s*IPC')
    out["time_ns"]      = _first_num_after_pipe(t, r'^\s*Time\s*\(ns\)')

    blk = re.search(r'Cache\s+L3\s*\|(?P<body>.*?)(?=\n\s*DRAM\s+summary\s*\||\Z)',
                    t, flags=re.IGNORECASE | re.S)
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

def to_float_or_nan(s):
    try:
        return float(s)
    except Exception:
        return float('nan')

def to_int_or_zero(s):
    try:
        return int(str(s).replace(',', '')) if s not in (None, '') else 0
    except Exception:
        return 0

# =========================
# SQLite helpers (exact LLC)
# =========================
def read_llc_exact_from_db(run_dir):
    """
    Return exact L3 stats from sim.stats.sqlite3 at ROI end:
      A_db (loads+stores), M_db (load-misses+store-misses),
      RH (l3_read_hits), WH (l3_write_hits),
      WB (l3_writebacks), EV (l3_evictions),
      M_custom (l3_misses custom counter)
    Missing DB -> None
    """
    db = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db):
        return None

    with sqlite3.connect(db) as conn:
        cur = conn.cursor()

        def sum_metrics(metrics):
            in_clause = ",".join(["?"] * len(metrics))
            sql = f"""
                SELECT IFNULL(SUM(v.value),0)
                FROM "values" v
                JOIN names    n ON v.nameid   = n.nameid
                JOIN prefixes p ON v.prefixid = p.prefixid
                WHERE p.prefixname = ?
                  AND n.objectname = 'L3'
                  AND n.metricname IN ({in_clause});
            """
            params = ("roi-end",) + tuple(metrics)
            cur.execute(sql, params)
            return int(cur.fetchone()[0])

        def one_metric(metric):
            sql = """
                SELECT IFNULL(SUM(v.value),0)
                FROM "values" v
                JOIN names    n ON v.nameid   = n.nameid
                JOIN prefixes p ON v.prefixid = p.prefixid
                WHERE p.prefixname = ?
                  AND n.objectname = 'L3'
                  AND n.metricname = ?;
            """
            cur.execute(sql, ("roi-end", metric))
            return int(cur.fetchone()[0])

        A_db     = sum_metrics(("loads","stores"))
        M_db     = sum_metrics(("load-misses","store-misses"))
        RH       = one_metric("l3_read_hits")
        WH       = one_metric("l3_write_hits")
        WB       = one_metric("l3_writebacks")
        EV       = one_metric("l3_evictions")
        M_custom = one_metric("l3_misses")

    return dict(A_db=A_db, M_db=M_db, RH=RH, WH=WH, WB=WB, EV=EV, M_custom=M_custom)

def read_llc_latency_from_db(run_dir):
    """
    Return L3 latency/time buckets (ns) from sim.stats.sqlite3 at ROI end.
      l3_total_latency_ns, l3_mshr_latency_ns, l3_snoop_latency_ns, l3_qbs_latency_ns
      l3_uncore_time_sum_ns = sum of uncore-time-* components (safe)
    Missing DB -> {}
    """
    db = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db):
        return {}
    with sqlite3.connect(db) as conn:
        cur = conn.cursor()

        # Use total() so large counters don't overflow 64-bit SUM()
        def one(metricname):
            cur.execute("""
                SELECT COALESCE(total(v.value), 0.0)
                FROM "values" v
                JOIN names    n ON v.nameid   = n.nameid
                JOIN prefixes p ON v.prefixid = p.prefixid
                WHERE p.prefixname = ?
                  AND n.objectname = 'L3'
                  AND n.metricname = ?;
            """, ("roi-end", metricname))
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else 0.0

        lat = {
            "l3_total_latency_ns":  one("total-latency"),
            "l3_mshr_latency_ns":   one("mshr-latency"),
            "l3_snoop_latency_ns":  one("snoop-latency"),
            "l3_qbs_latency_ns":    one("qbs-query-latency"),
        }

        # Sum all uncore-time-* safely (again, use total())
        cur.execute("""
            SELECT n.metricname, COALESCE(total(v.value), 0.0)
            FROM "values" v
            JOIN names    n ON v.nameid   = n.nameid
            JOIN prefixes p ON v.prefixid = p.prefixid
            WHERE p.prefixname = ?
              AND n.objectname = 'L3'
              AND n.metricname LIKE 'uncore-time-%'
            GROUP BY n.metricname;
        """, ("roi-end",))
        rows = cur.fetchall()
        lat["l3_uncore_time_sum_ns"] = float(sum(val for _, val in rows)) if rows else 0.0

    return lat


def read_uncore_requests(run_dir):
    db = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db):
        return 0
    with sqlite3.connect(db) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT IFNULL(SUM(v.value),0)
            FROM "values" v
            JOIN names    n ON v.nameid   = n.nameid
            JOIN prefixes p ON v.prefixid = p.prefixid
            WHERE p.prefixname='roi-end'
              AND n.objectname='L3'
              AND n.metricname='uncore-requests';
        """)
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

def parse_llc_hit_cycles(run_dir):
    """
    Read configured LLC hit cycles from sim.cfg.
    Works with either:
      [perf_model/l3_cache/llc]
      read_hit_latency_cycles = 6
      write_hit_latency_cycles = 17
    or fully-qualified lines.
    """
    cfg = os.path.join(run_dir, "sim.cfg")
    rd = wr = None
    sect = None
    if os.path.exists(cfg):
        try:
            with open(cfg, "r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith(("#",";")):
                        continue
                    m = re.match(r'\[(.+?)\]', line)
                    if m:
                        sect = m.group(1).strip()
                        continue
                    m = re.match(r'perf_model/l3_cache/llc/read_hit_latency_cycles\s*=\s*([0-9]+)', line)
                    if m: rd = int(m.group(1)); continue
                    m = re.match(r'perf_model/l3_cache/llc/write_hit_latency_cycles\s*=\s*([0-9]+)', line)
                    if m: wr = int(m.group(1)); continue
                    if sect and sect.lower() == 'perf_model/l3_cache/llc':
                        m = re.match(r'read_hit_latency_cycles\s*=\s*([0-9]+)', line)
                        if m: rd = int(m.group(1)); continue
                        m = re.match(r'write_hit_latency_cycles\s*=\s*([0-9]+)', line)
                        if m: wr = int(m.group(1)); continue
        except Exception:
            pass
    return rd, wr

def avg_l3_hit_ns(rd_cyc, wr_cyc, RH, WH, period_ns):
    """
    Weighted avg hit time in ns:
      (RH*rd_cyc + WH*wr_cyc) * period_ns / (RH+WH)
    """
    try:
        total = (RH or 0) + (WH or 0)
        if not rd_cyc or not wr_cyc or total <= 0 or not period_ns or period_ns <= 0:
            return None
        return ((RH*rd_cyc + WH*wr_cyc) * period_ns) / total
    except Exception:
        return None

def period_ns_from_parsed(parsed):
    try:
        cyc = int(parsed.get("cycles") or 0)
        tns = int(parsed.get("time_ns") or 0)
        return (tns / cyc) if (cyc > 0) else None
    except Exception:
        return None

# =========================
# Energy parsing & math
# =========================
def parse_llc_energy_consts(run_dir):
    """
    Try to read LLC energy constants from (in order):
      1) sim.cfg
      2) sim.info (Sniper's run summary with -g flags)
      3) sim.inf  (some runs write this truncated name)

    Returns dict in nJ/W on success, else None.
    """
    def _scan_text(txt):
        # works for both "e_read_hit_pJ=397" and "--perf_model/.../e_read_hit_pJ=397 "
        patt = {
            "E_hit_nJ":  r'perf_model/l3_cache/llc/e_read_hit_pJ\s*=\s*([0-9.]+)',
            "E_write_nJ":r'perf_model/l3_cache/llc/e_write_hit_pJ\s*=\s*([0-9.]+)',
            "E_miss_nJ": r'perf_model/l3_cache/llc/e_miss_pJ\s*=\s*([0-9.]+)',
            "P_leak_W":  r'perf_model/l3_cache/llc/p_leak_mW\s*=\s*([0-9.]+)',
        }
        out = {}
        for k, rgx in patt.items():
            m = re.search(rgx, txt)
            if not m:
                return None
            val = float(m.group(1))
            if k == "P_leak_W":
                out[k] = val / 1000.0  # mW -> W
            else:
                out[k] = val / 1000.0  # pJ -> nJ
        return out

    # 1) sim.cfg
    cfg = os.path.join(run_dir, "sim.cfg")
    if os.path.exists(cfg):
        try:
            with open(cfg, "r", encoding="utf-8", errors="ignore") as f:
                vals = _scan_text(f.read())
                if vals: return vals
        except Exception:
            pass

    # 2) sim.info
    info = os.path.join(run_dir, "sim.info")
    if os.path.exists(info):
        try:
            with open(info, "r", encoding="utf-8", errors="ignore") as f:
                vals = _scan_text(f.read())
                if vals: return vals
        except Exception:
            pass

    # 3) sim.inf (seen in your logs for a few benches)
    info_alt = os.path.join(run_dir, "sim.inf")
    if os.path.exists(info_alt):
        try:
            with open(info_alt, "r", encoding="utf-8", errors="ignore") as f:
                vals = _scan_text(f.read())
                if vals: return vals
        except Exception:
            pass

    return None

def energy_bounds(E_hit_nJ, E_miss_nJ, E_write_nJ, P_leak_W, T_s, acc, mis):
    hits = max(acc - mis, 0)
    dyn_lo_nJ = E_hit_nJ * hits + E_miss_nJ * mis   # assume all hits are reads
    dyn_hi_nJ = E_write_nJ * hits + E_miss_nJ * mis # assume all hits are writes
    eleak_J   = P_leak_W * (T_s if T_s == T_s else 0.0)
    return (dyn_lo_nJ*1e-9 + eleak_J, dyn_hi_nJ*1e-9 + eleak_J), (dyn_lo_nJ, dyn_hi_nJ), eleak_J

def energy_exact_from_counts(E_hit_nJ, E_miss_nJ, E_write_nJ, P_leak_W, T_s, RH, WH, M):
    dyn_nJ = E_hit_nJ*RH + E_write_nJ*WH + E_miss_nJ*M
    E = dyn_nJ*1e-9 + P_leak_W*(T_s if T_s == T_s else 0.0)
    return dyn_nJ, E

def ed2p(E_J, T_s):
    if not (E_J == E_J) or not (T_s == T_s):
        return float('nan')
    return E_J * (T_s ** 2)

# =========================
# CSV writer (overwrite)
# =========================
def write_csv_overwrite(path, header, rows):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)

# =========================
# Main
# =========================
def main():
    ap = argparse.ArgumentParser(description="Write energy_bounds.csv and summary.csv to <OUT_ROOT>/output_<bench>/")
    ap.add_argument("sram_simout", help="Path to SRAM sim.out")
    ap.add_argument("jans_simout", help="Path to JanS sim.out")
    args = ap.parse_args()

    sram_simout = os.path.abspath(args.sram_simout)
    jans_simout = os.path.abspath(args.jans_simout)

    # hard fail on bad paths
    for p in (sram_simout, jans_simout):
        if not os.path.isfile(p):
            raise SystemExit(f"[ERR] sim.out not found: {p}")

    sram_dir = os.path.dirname(sram_simout)
    jans_dir = os.path.dirname(jans_simout)

    out_dir = infer_bench_root_from_run_dir(sram_dir)
    ensure_dir(out_dir)

    bench_name, n_m = extract_bench_name_and_nm(sram_dir)

    sram_parsed, _ = parse_simout_full(sram_simout)
    jans_parsed, _ = parse_simout_full(jans_simout)

    T_s = to_float_or_nan(sram_parsed.get("time_ns")) / 1e9 if sram_parsed.get("time_ns") else float('nan')
    T_n = to_float_or_nan(jans_parsed.get("time_ns")) / 1e9 if jans_parsed.get("time_ns") else float('nan')

    A_s_txt = to_int_or_zero(sram_parsed.get("l3_acc"))
    M_s_txt = to_int_or_zero(sram_parsed.get("l3_miss"))
    A_n_txt = to_int_or_zero(jans_parsed.get("l3_acc"))
    M_n_txt = to_int_or_zero(jans_parsed.get("l3_miss"))

    s_db = read_llc_exact_from_db(sram_dir)
    n_db = read_llc_exact_from_db(jans_dir)
    s_lat = read_llc_latency_from_db(sram_dir)
    n_lat = read_llc_latency_from_db(jans_dir)

    s_period_ns = period_ns_from_parsed(sram_parsed)
    n_period_ns = period_ns_from_parsed(jans_parsed)
    s_rd_cyc, s_wr_cyc = parse_llc_hit_cycles(sram_dir)
    n_rd_cyc, n_wr_cyc = parse_llc_hit_cycles(jans_dir)
    s_avg_hit_ns = avg_l3_hit_ns(s_rd_cyc, s_wr_cyc, s_db["RH"] if s_db else None, s_db["WH"] if s_db else None, s_period_ns)
    n_avg_hit_ns = avg_l3_hit_ns(n_rd_cyc, n_wr_cyc, n_db["RH"] if n_db else None, n_db["WH"] if n_db else None, n_period_ns)

    # uncore requests and per-request time
    s_unc_reqs = read_uncore_requests(sram_dir)
    n_unc_reqs = read_uncore_requests(jans_dir)
    s_avg_unc_ns = (float(s_lat.get("l3_uncore_time_sum_ns", 0)) / s_unc_reqs) if s_unc_reqs else ""
    n_avg_unc_ns = (float(n_lat.get("l3_uncore_time_sum_ns", 0)) / n_unc_reqs) if n_unc_reqs else ""

    # --- Pick energy constants (sim.cfg overrides -> defaults) ---
    s_consts = parse_llc_energy_consts(sram_dir) or SRAM_DEFAULT
    n_consts = parse_llc_energy_consts(jans_dir) or JANS_DEFAULT

    # Bounds
    (s_Elo, s_Ehi), (sd_lo, sd_hi), s_leakJ = energy_bounds(
        **s_consts, T_s=T_s, acc=A_s_txt, mis=M_s_txt)
    (n_Elo, n_Ehi), (nd_lo, nd_hi), n_leakJ = energy_bounds(
        **n_consts, T_s=T_n, acc=A_n_txt, mis=M_n_txt)

    # Exact energies
    s_dyn_exact = s_E_exact = float('nan')
    n_dyn_exact = n_E_exact = float('nan')
    s_exact_src = n_exact_src = ""
    s_leakW = s_consts["P_leak_W"]
    n_leakW = n_consts["P_leak_W"]

    def mismatch_note(d):
        if d is None: return "sqlite_missing"
        A, M, RH, WH = d["A_db"], d["M_db"], d["RH"], d["WH"]
        diff = (A - M) - (RH + WH)
        return "ok" if diff == 0 else f"warn_A-M!=RH+WH(diff={diff})"

    s_note = mismatch_note(s_db)
    n_note = mismatch_note(n_db)

    if s_db is not None:
        s_dyn_exact, s_E_exact = energy_exact_from_counts(
            s_consts["E_hit_nJ"], s_consts["E_miss_nJ"], s_consts["E_write_nJ"], s_leakW, T_s,
            s_db["RH"], s_db["WH"], s_db["M_db"]
        )
        s_exact_src = "sqlite"

    if n_db is not None:
        n_dyn_exact, n_E_exact = energy_exact_from_counts(
            n_consts["E_hit_nJ"], n_consts["E_miss_nJ"], n_consts["E_write_nJ"], n_leakW, T_n,
            n_db["RH"], n_db["WH"], n_db["M_db"]
        )
        n_exact_src = "sqlite"

    # ===== energy_bounds.csv =====
    energy_path = os.path.join(out_dir, "energy_bounds.csv")
    energy_header = [
        "benchmark","n_m","config",
        "time_s",
        "l3_accesses","l3_misses_db","l3_read_hits","l3_write_hits",
        "l3_writebacks","l3_evictions",
        "leak_W","leak_J",
        "dyn_exact_nJ","energy_exact_J","ed2p_exact_J_s2",
        "dyn_lower_nJ","dyn_upper_nJ",
        "energy_lower_J","energy_upper_J",
        "ed2p_lower_J_s2","ed2p_upper_J_s2",
        "energy_scope","exact_source","notes"
    ]
    def row_for(cfg, T, A_txt, M_txt, db, leakW, leakJ, dyn_exact, E_exact, dyn_pair, E_bounds, note, exact_src):
        A = db["A_db"] if db else ""
        M = db["M_db"] if db else ""
        RH = db["RH"] if db else ""
        WH = db["WH"] if db else ""
        WB = db["WB"] if db else ""
        EV = db["EV"] if db else ""
        dyn_lo, dyn_hi = dyn_pair
        Elo, Ehi = E_bounds
        return [
            bench_name, (n_m or ""), cfg,
            f"{T:.6f}",
            str(A if A != "" else A_txt), str(M if M != "" else M_txt), str(RH), str(WH),
            str(WB), str(EV),
            f"{leakW:.3f}", f"{leakJ:.6f}",
            (f"{dyn_exact:.0f}" if dyn_exact == dyn_exact else ""),
            (f"{E_exact:.6f}" if E_exact == E_exact else ""),
            (f"{ed2p(E_exact,T):.9e}" if E_exact == E_exact else ""),
            f"{dyn_lo:.0f}", f"{dyn_hi:.0f}",
            f"{Elo:.6f}", f"{Ehi:.6f}",
            f"{ed2p(Elo,T):.9e}", f"{ed2p(Ehi,T):.9e}",
            "llc_only", exact_src, note
        ]

    s_row = row_for("SRAM", T_s, A_s_txt, M_s_txt, s_db, s_leakW, s_leakJ, s_dyn_exact, s_E_exact, (sd_lo, sd_hi), (s_Elo, s_Ehi), s_note, s_exact_src)
    n_row = row_for("JanS", T_n, A_n_txt, M_n_txt, n_db, n_leakW, n_leakJ, n_dyn_exact, n_E_exact, (nd_lo, nd_hi), (n_Elo, n_Ehi), n_note, n_exact_src)
    write_csv_overwrite(energy_path, energy_header, [s_row, n_row])
    print(f"[OK] wrote {energy_path}")

    # ===== summary.csv =====
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    summary_path = os.path.join(out_dir, "summary.csv")
    summary_header = [
        "timestamp_utc","benchmark","n_m","config",
        "instructions","cycles","ipc","time_ns",
        "l3_acc_text","l3_miss_text","l3_miss_rate_pct",
        "dram_acc","dram_lat_value","dram_lat_unit",
        "outdir",
        "l3_accesses_db","l3_misses_db","l3_read_hits","l3_write_hits","l3_writebacks","l3_evictions","l3_misses_custom",
        "l3_total_latency_ns","l3_mshr_latency_ns","l3_snoop_latency_ns","l3_qbs_latency_ns","l3_uncore_time_sum_ns",
        "l3_uncore_requests","avg_uncore_time_per_req_ns",
        "rd_hit_cycles","wr_hit_cycles","core_period_ns","avg_l3_hit_ns"
    ]
    def srow(cfg, parsed, run_dir, db, lat, unc_reqs, rd_cyc, wr_cyc, period_ns, avg_hit_ns, avg_unc_ns):
        return [
            ts, bench_name, (n_m or ""), cfg,
            parsed.get("instructions") or "",
            parsed.get("cycles") or "",
            parsed.get("ipc") or "",
            parsed.get("time_ns") or "",
            parsed.get("l3_acc") or "",
            parsed.get("l3_miss") or "",
            parsed.get("l3_miss_rate_pct") or "",
            parsed.get("dram_acc") or "",
            parsed.get("dram_lat_value") or "",
            parsed.get("dram_lat_unit") or "",
            run_dir,
            str(db["A_db"]) if db else "",
            str(db["M_db"]) if db else "",
            str(db["RH"])   if db else "",
            str(db["WH"])   if db else "",
            str(db["WB"])   if db else "",
            str(db["EV"])   if db else "",
            str(db["M_custom"]) if db else "",
            str(lat.get("l3_total_latency_ns","")),
            str(lat.get("l3_mshr_latency_ns","")),
            str(lat.get("l3_snoop_latency_ns","")),
            str(lat.get("l3_qbs_latency_ns","")),
            str(lat.get("l3_uncore_time_sum_ns","")),
            str(unc_reqs),
            (f"{avg_unc_ns:.6f}" if isinstance(avg_unc_ns, (float,int)) and avg_unc_ns != "" else ""),
            str(rd_cyc if rd_cyc is not None else ""),
            str(wr_cyc if wr_cyc is not None else ""),
            (f"{period_ns:.9f}" if period_ns else ""),
            (f"{avg_hit_ns:.6f}" if avg_hit_ns else "")
        ]

    write_csv_overwrite(summary_path, summary_header, [
        srow("SRAM", sram_parsed, sram_dir, s_db, s_lat, s_unc_reqs, s_rd_cyc, s_wr_cyc, s_period_ns, s_avg_hit_ns, s_avg_unc_ns),
        srow("JanS", jans_parsed, jans_dir, n_db, n_lat, n_unc_reqs, n_rd_cyc, n_wr_cyc, n_period_ns, n_avg_hit_ns, n_avg_unc_ns),
    ])
    print(f"[OK] wrote {summary_path}")

    # ===== console summary =====
    print("\n==== Post-run LLC energy ====")
    def pretty(cfg, T, A_txt, M_txt, E_bounds, leakJ, E_exact, note, consts_src):
        Elo, Ehi = E_bounds
        src = "sim.cfg" if consts_src else "defaults"
        print(f"{cfg}: time={T:.6f}s  L3_txt_acc/miss={A_txt}/{M_txt}  "
              f"-> E_bounds={Elo:.6f}..{Ehi:.6f} J  (leak={leakJ:.6f} J)"
              f"{'  |  E_exact='+format(E_exact,'.6f')+' J' if E_exact==E_exact else ''}"
              f"  [stats:{note}; consts:{src}]")

    pretty("SRAM", T_s, A_s_txt, M_s_txt, (s_Elo, s_Ehi), s_leakJ, s_E_exact, s_note,
           parse_llc_energy_consts(sram_dir) is not None)
    pretty("JanS", T_n, A_n_txt, M_n_txt, (n_Elo, n_Ehi), n_leakJ, n_E_exact, n_note,
           parse_llc_energy_consts(jans_dir) is not None)

if __name__ == "__main__":
    main()
