#!/usr/bin/env python3
"""
Compute LLC-only energy/ED^2P for SRAM vs JanS.

- Reads timing + coarse L3 stats from each run's sim.out
- Reads exact L3 breakdown from sim.stats.sqlite3 (roi-end)
- Writes CSVs into: <RUN_ROOT>/output_<bench>/
    * energy_bounds.csv   (includes exact energy + bounds)
    * summary.csv         (text + sqlite stats side-by-side)

Usage:
  python3 scripts/energy_ed2p_v4.py <sram_simout> <jans_simout>

Notes:
  * Energy scope is LLC-only (nJ/event + W leakage).
  * DRAM latency from sim.out is recorded as value + unit (no conversion).
"""

import argparse, csv, os, re, sqlite3
from datetime import datetime, timezone

# =========================
# Constants (LLC model)
# =========================
SRAM = dict(E_hit_nJ=0.565, E_miss_nJ=0.011, E_write_nJ=0.537, P_leak_W=3.438)
JANS = dict(E_hit_nJ=0.188, E_miss_nJ=0.077, E_write_nJ=2.305, P_leak_W=0.048)

# =========================
# FS helpers
# =========================
def ensure_dir(path):
    os.makedirs(path, exist_ok=True)

def infer_bench_root_from_run_dir(run_dir):
    run_root = os.path.dirname(run_dir)
    leaf     = os.path.basename(run_dir)
    base     = re.sub(r'_(sram|jans)[^/]*', '', leaf, flags=re.IGNORECASE)
    base     = re.sub(r'_\d+M$', '', base)
    return os.path.join(run_root, f"output_{base}")

def extract_bench_name_and_nm(run_dir):
    leaf = os.path.basename(run_dir)
    bench = re.sub(r'_(sram|jans)[^/]*', '', leaf, flags=re.IGNORECASE)
    m = re.search(r'_(\d+)M$', leaf)
    n_m = m.group(1) if m else None
    bench = re.sub(r'_\d+M$', '', bench)
    return bench, n_m

# =========================
# sim.out parser
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
    t = _read_text(path)
    out = {}

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
        if dlat: dram_lat_val, dram_lat_unit = dlat.group(1), dlat.group(2)

    out["dram_acc"] = dram_acc
    out["dram_lat_value"] = dram_lat_val
    out["dram_lat_unit"]  = dram_lat_unit
    return out, t

def to_float_or_nan(s):
    try:    return float(s)
    except: return float('nan')

def to_int_or_zero(s):
    try:
        return int(str(s).replace(',', '')) if s not in (None, '') else 0
    except:
        return 0

# =========================
# SQLite helpers
# =========================
def _sum_metric(cur, metricnames):
    marks = ",".join(["?"] * len(metricnames))
    sql = f"""
        SELECT IFNULL(SUM(v.value),0)
        FROM "values" v
        JOIN names    n ON v.nameid   = n.nameid
        JOIN prefixes p ON v.prefixid = p.prefixid
        WHERE p.prefixname = ?
          AND n.objectname = 'L3'
          AND n.metricname IN ({marks});
    """
    cur.execute(sql, ("roi-end", *metricnames))
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0

def _one_metric(cur, metric):
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
    row = cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0

def read_llc_exact_from_db(run_dir):
    db = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db): return None
    with sqlite3.connect(db) as conn:
        cur = conn.cursor()
        A_db = _sum_metric(cur, ("loads","stores"))
        M_db = _sum_metric(cur, ("load-misses","store-misses"))
        RH   = _one_metric(cur, "l3_read_hits")
        WH   = _one_metric(cur, "l3_write_hits")
        WB   = _one_metric(cur, "l3_writebacks")
        EV   = _one_metric(cur, "l3_evictions")
        MC   = _one_metric(cur, "l3_misses")
        UR   = _one_metric(cur, "uncore-requests")
    return dict(A_db=A_db, M_db=M_db, RH=RH, WH=WH, WB=WB, EV=EV, M_custom=MC, UR=UR)

def read_llc_latency_from_db(run_dir):
    db = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db): return {}
    with sqlite3.connect(db) as conn:
        cur = conn.cursor()
        return {
            "l3_total_latency_ns":  _one_metric(cur, "total-latency"),
            "l3_mshr_latency_ns":   _one_metric(cur, "mshr-latency"),
            "l3_snoop_latency_ns":  _one_metric(cur, "snoop-latency"),
            "l3_qbs_latency_ns":    _one_metric(cur, "qbs-query-latency"),
        }

# =========================
# Hit cycles and timing
# =========================
def parse_llc_hit_cycles(run_dir):
    cfg = os.path.join(run_dir, "sim.cfg")
    rd = wr = None
    if os.path.exists(cfg):
        try:
            txt = open(cfg, "r", encoding="utf-8", errors="ignore").read()
            # section-local (inside [perf_model/l3_cache/llc]) also works with these
            m = re.search(r'^\s*(?:perf_model/l3_cache/llc/)?read_hit_latency_cycles\s*=\s*([0-9]+)', txt, flags=re.M)
            if m: rd = int(m.group(1))
            m = re.search(r'^\s*(?:perf_model/l3_cache/llc/)?write_hit_latency_cycles\s*=\s*([0-9]+)', txt, flags=re.M)
            if m: wr = int(m.group(1))
        except: pass
    return rd, wr

def core_period_ns(parsed):
    try:
        cyc = int(parsed.get("cycles") or 0)
        tns = int(parsed.get("time_ns") or 0)
        return (tns / cyc) if (cyc > 0) else None
    except: return None

def avg_l3_hit_ns(rd_cyc, wr_cyc, RH, WH, period_ns):
    try:
        tot = (RH or 0) + (WH or 0)
        if not rd_cyc or not wr_cyc or tot <= 0 or not period_ns or period_ns <= 0:
            return None
        return ((RH*rd_cyc + WH*wr_cyc) * period_ns) / tot
    except: return None

# =========================
# Energy math
# =========================
def energy_bounds(E_hit_nJ, E_miss_nJ, E_write_nJ, P_leak_W, T_s, acc, mis):
    hits = max((acc or 0) - (mis or 0), 0)
    dyn_lo_nJ = E_hit_nJ  * hits + E_miss_nJ * (mis or 0)   # hits=reads
    dyn_hi_nJ = E_write_nJ* hits + E_miss_nJ * (mis or 0)   # hits=writes
    eleak_J   = P_leak_W * (T_s if T_s == T_s else 0.0)
    E_lo = dyn_lo_nJ * 1e-9 + eleak_J
    E_hi = dyn_hi_nJ * 1e-9 + eleak_J
    return (E_lo, E_hi), (dyn_lo_nJ, dyn_hi_nJ), eleak_J

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
    ap = argparse.ArgumentParser(description="Write energy_bounds.csv and summary.csv to <RUN_ROOT>/output_<bench>/")
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

    # sim.out text
    sram_parsed, _ = parse_simout_full(sram_simout)
    jans_parsed, _ = parse_simout_full(jans_simout)

    # timing
    T_s = to_float_or_nan(sram_parsed.get("time_ns")) / 1e9 if sram_parsed.get("time_ns") else float('nan')
    T_n = to_float_or_nan(jans_parsed.get("time_ns")) / 1e9 if jans_parsed.get("time_ns") else float('nan')

    # coarse A/M from text (fallback if DB missing)
    A_s_txt = to_int_or_zero(sram_parsed.get("l3_acc"))
    M_s_txt = to_int_or_zero(sram_parsed.get("l3_miss"))
    A_n_txt = to_int_or_zero(jans_parsed.get("l3_acc"))
    M_n_txt = to_int_or_zero(jans_parsed.get("l3_miss"))

    # sqlite exact
    s_db  = read_llc_exact_from_db(sram_dir)
    n_db  = read_llc_exact_from_db(jans_dir)
    s_lat = read_llc_latency_from_db(sram_dir)
    n_lat = read_llc_latency_from_db(jans_dir)

    # cycles from config
    s_rd_cyc, s_wr_cyc = parse_llc_hit_cycles(sram_dir)
    n_rd_cyc, n_wr_cyc = parse_llc_hit_cycles(jans_dir)

    # core period & avg hit latency
    s_period_ns = core_period_ns(sram_parsed)
    n_period_ns = core_period_ns(jans_parsed)
    s_avg_hit_ns = avg_l3_hit_ns(s_rd_cyc, s_wr_cyc, s_db["RH"] if s_db else None, s_db["WH"] if s_db else None, s_period_ns)
    n_avg_hit_ns = avg_l3_hit_ns(n_rd_cyc, n_wr_cyc, n_db["RH"] if n_db else None, n_db["WH"] if n_db else None, n_period_ns)

    # bounds (use text A/M so we still produce something if sqlite missing)
    (s_Elo, s_Ehi), (sd_lo, sd_hi), s_leakJ = energy_bounds(**SRAM, T_s=T_s, acc=A_s_txt, mis=M_s_txt)
    (n_Elo, n_Ehi), (nd_lo, nd_hi), n_leakJ = energy_bounds(**JANS, T_s=T_n, acc=A_n_txt, mis=M_n_txt)

    # exact energies if DB available
    s_dyn_exact = s_E_exact = float('nan')
    n_dyn_exact = n_E_exact = float('nan')
    s_exact_src = n_exact_src = ""
    def note_for(d):
        if d is None: return "sqlite_missing"
        A, M, RH, WH = d["A_db"], d["M_db"], d["RH"], d["WH"]
        diff = (A - M) - (RH + WH)
        return "ok" if diff == 0 else f"warn_A-M!=RH+WH(diff={diff})"
    s_note = note_for(s_db); n_note = note_for(n_db)

    if s_db:
        s_dyn_exact, s_E_exact = energy_exact_from_counts(
            SRAM["E_hit_nJ"], SRAM["E_miss_nJ"], SRAM["E_write_nJ"], SRAM["P_leak_W"], T_s,
            s_db["RH"], s_db["WH"], s_db["M_db"]
        ); s_exact_src = "sqlite"
    if n_db:
        n_dyn_exact, n_E_exact = energy_exact_from_counts(
            JANS["E_hit_nJ"], JANS["E_miss_nJ"], JANS["E_write_nJ"], JANS["P_leak_W"], T_n,
            n_db["RH"], n_db["WH"], n_db["M_db"]
        ); n_exact_src = "sqlite"

    # ===== energy_bounds.csv =====
    energy_path = os.path.join(out_dir, "energy_bounds.csv")
    energy_header = [
        "benchmark","n_m","config","time_s",
        "l3_accesses","l3_misses_db","l3_read_hits","l3_write_hits","l3_writebacks","l3_evictions",
        "leak_W","leak_J",
        "dyn_exact_nJ","energy_exact_J","ed2p_exact_J_s2",
        "dyn_lower_nJ","dyn_upper_nJ","energy_lower_J","energy_upper_J","ed2p_lower_J_s2","ed2p_upper_J_s2",
        "energy_scope","exact_source","notes"
    ]
    def energy_row(cfg, T, A_txt, M_txt, db, leakW, leakJ, dyn_exact, E_exact, dyn_lo_nJ, dyn_hi_nJ, E_lo, E_hi, note, exact_src):
        A = (db["A_db"] if db else A_txt)
        M = (db["M_db"] if db else M_txt)
        RH = (db["RH"] if db else "")
        WH = (db["WH"] if db else "")
        WB = (db["WB"] if db else "")
        EV = (db["EV"] if db else "")
        Elo, Ehi = sorted((E_lo, E_hi))
        return [
            bench_name, (n_m or ""), cfg, f"{(T if T==T else 0.0):.6f}",
            str(A), str(M), str(RH), str(WH), str(WB), str(EV),
            f"{leakW:.3f}", f"{leakJ:.6f}",
            (f"{dyn_exact:.0f}" if dyn_exact == dyn_exact else ""),
            (f"{E_exact:.6f}"   if E_exact   == E_exact   else ""),
            (f"{ed2p(E_exact,T):.9e}" if E_exact==E_exact else ""),
            f"{dyn_lo_nJ:.0f}", f"{dyn_hi_nJ:.0f}", f"{E_lo:.6f}", f"{E_hi:.6f}",
            f"{ed2p(E_lo,T):.9e}", f"{ed2p(E_hi,T):.9e}",
            "llc_only", exact_src, note
        ]

    s_row = energy_row("SRAM", T_s, A_s_txt, M_s_txt, s_db, SRAM["P_leak_W"], s_leakJ,
                       s_dyn_exact, s_E_exact, sd_lo, sd_hi, s_Elo, s_Ehi, s_note, s_exact_src)
    n_row = energy_row("JanS", T_n, A_n_txt, M_n_txt, n_db, JANS["P_leak_W"], n_leakJ,
                       n_dyn_exact, n_E_exact, nd_lo, nd_hi, n_Elo, n_Ehi, n_note, n_exact_src)

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
        "l3_total_latency_ns","l3_mshr_latency_ns","l3_snoop_latency_ns","l3_qbs_latency_ns",
        "l3_uncore_requests",
        "rd_hit_cycles","wr_hit_cycles","core_period_ns","avg_l3_hit_ns"
    ]
    def srow(cfg, parsed, run_dir, db, lat, rd_cyc, wr_cyc, period_ns, avg_hit_ns):
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
            (str(db["A_db"]) if db else ""),
            (str(db["M_db"]) if db else ""),
            (str(db["RH"])   if db else ""),
            (str(db["WH"])   if db else ""),
            (str(db["WB"])   if db else ""),
            (str(db["EV"])   if db else ""),
            (str(db["M_custom"]) if db else ""),
            str(lat.get("l3_total_latency_ns","")),
            str(lat.get("l3_mshr_latency_ns","")),
            str(lat.get("l3_snoop_latency_ns","")),
            str(lat.get("l3_qbs_latency_ns","")),
            (str(db["UR"]) if db and "UR" in db else ""),
            (str(rd_cyc) if rd_cyc is not None else ""),
            (str(wr_cyc) if wr_cyc is not None else ""),
            (f"{period_ns:.9f}" if period_ns else ""),
            (f"{avg_hit_ns:.6f}" if avg_hit_ns else "")
        ]

    write_csv_overwrite(summary_path, summary_header, [
        srow("SRAM", sram_parsed, sram_dir, s_db, s_lat, s_rd_cyc, s_wr_cyc, s_period_ns, s_avg_hit_ns),
        srow("JanS", jans_parsed, jans_dir, n_db, n_lat, n_rd_cyc, n_wr_cyc, n_period_ns, n_avg_hit_ns),
    ])
    print(f"[OK] wrote {summary_path}")

    # console summary
    print("\n==== Post-run LLC energy ====")
    def pretty(cfg, T, A_txt, M_txt, E_lo, E_hi, leakJ, E_exact, note):
        Elo, Ehi = sorted((E_lo, E_hi))
        print(f"{cfg}: time={(T if T==T else 0.0):.6f}s  L3_txt_acc/miss={A_txt}/{M_txt}  "
              f"-> E_bounds={E_lo:.6f}..{E_hi:.6f} J  (leak={leakJ:.6f} J)"
              f"{'  |  E_exact='+format(E_exact,'.6f')+' J' if E_exact==E_exact else ''}"
              f"  [{note}]")
    pretty("SRAM", T_s, A_s_txt, M_s_txt, s_Elo, s_Ehi, s_leakJ, s_E_exact, s_note)
    pretty("JanS", T_n, A_n_txt, M_n_txt, n_Elo, n_Ehi, n_leakJ, n_E_exact, n_note)

if __name__ == "__main__":
    main()

