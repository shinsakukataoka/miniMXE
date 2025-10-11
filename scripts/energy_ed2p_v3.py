#!/usr/bin/env python3
"""
Compute LLC-only energy/ED^2P for SRAM vs JanS (or any "tech" run in the JanS slot).

What this script does:
- Reads timing + coarse L3 stats from each run's sim.out
- Reads exact L3 hit/miss breakdown from sim.stats.sqlite3 (ROI delta = roi-end - roi-begin)
- Includes all L3 slices for hit counters (objectname LIKE 'L3%': local + remote/forwarded)
- Reads LLC energy constants (pJ/mW) from each run's sim.cfg if present
- Weighted LLC hit cycles if sim.cfg defines llc.next_read, using ROI hit mix
- Optionally rebalances missing hits (coherency upgrades) into write-hits for energy
- Writes CSVs into: <OUT_ROOT>/output_<bench>/
    * energy_bounds.csv   (includes exact energy + bounds)
    * summary.csv         (text + sqlite stats side-by-side + avg L3 hit ns)

Usage:
  python3 scripts/energy_ed2p_v3.py <sram_simout> <jans_simout>

Notes:
  * Energy scope is LLC-only (nJ/event + W leakage).
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
    Return exact L3 stats from sim.stats.sqlite3 over the ROI (end - begin):
      A_db (loads+stores on L3), M_db (load-misses+store-misses on L3),
      RH, WH = l3_*_hits summed across ALL L3 slices (objectname LIKE 'L3%'),
      WB, EV, M_custom on L3,
      plus debug fields: RH_local/RH_remote/WH_local/WH_remote and prefetch counters and coh_upgrades.
    """
    db = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db):
        return None

    with sqlite3.connect(db) as conn:
        cur = conn.cursor()

        # ROI-delta helpers
        def sum_delta_int_L3(metrics):
            placeholders = ",".join(["?"] * len(metrics))
            sql = """
                SELECT COALESCE(SUM(
                    CASE p.prefixname
                        WHEN 'roi-end'   THEN v.value
                        WHEN 'roi-begin' THEN -v.value
                        ELSE 0
                    END
                ), 0)
                FROM "values" v
                JOIN names    n ON v.nameid   = n.nameid
                JOIN prefixes p ON v.prefixid = p.prefixid
                WHERE n.objectname='L3'
                  AND n.metricname IN ({})
                  AND p.prefixname IN ('roi-begin','roi-end');
            """.format(placeholders)
            cur.execute(sql, tuple(metrics))
            val = cur.fetchone()[0]
            try:
                return int(val or 0)
            except Exception:
                return int(float(val or 0.0))

        def one_delta_int_L3(metric):
            return sum_delta_int_L3((metric,))

        def sum_delta_int_like(metric, obj_like):
            cur.execute("""
                SELECT COALESCE(SUM(
                    CASE p.prefixname
                        WHEN 'roi-end'   THEN v.value
                        WHEN 'roi-begin' THEN -v.value
                        ELSE 0
                    END
                ), 0)
                FROM "values" v
                JOIN names    n ON v.nameid   = n.nameid
                JOIN prefixes p ON v.prefixid = p.prefixid
                WHERE n.metricname = ?
                  AND n.objectname LIKE ?
                  AND p.prefixname IN ('roi-begin','roi-end');
            """, (metric, obj_like))
            val = cur.fetchone()[0]
            try:
                return int(val or 0)
            except Exception:
                return int(float(val or 0.0))

        # Accesses/misses from L3 only (same as before)
        A_db     = sum_delta_int_L3(("loads", "stores"))
        M_db     = sum_delta_int_L3(("load-misses", "store-misses"))

        # Hits from ALL L3 slices (L3, L3.next_read, etc.)
        RH_local = one_delta_int_L3("l3_read_hits")
        RH_all   = sum_delta_int_like("l3_read_hits",  "L3%")
        RH_remote = max(RH_all - RH_local, 0)

        WH_local = one_delta_int_L3("l3_write_hits")
        WH_all   = sum_delta_int_like("l3_write_hits", "L3%")
        WH_remote = max(WH_all - WH_local, 0)

        RH = RH_all
        WH = WH_all

        # Other L3-only counters
        WB          = one_delta_int_L3("l3_writebacks")
        EV          = one_delta_int_L3("l3_evictions")
        M_custom    = one_delta_int_L3("l3_misses")
        coh_upgrades= one_delta_int_L3("coherency-upgrades")

        # Optional debug (may be zero on many benches)
        hits_prefetch   = one_delta_int_L3("hits-prefetch")
        loads_prefetch  = one_delta_int_L3("loads-prefetch")
        stores_prefetch = one_delta_int_L3("stores-prefetch")
        prefetches      = one_delta_int_L3("prefetches")

    return dict(
        A_db=A_db, M_db=M_db, RH=RH, WH=WH, WB=WB, EV=EV, M_custom=M_custom,
        RH_local=RH_local, RH_remote=RH_remote, WH_local=WH_local, WH_remote=WH_remote,
        hits_prefetch=hits_prefetch, loads_prefetch=loads_prefetch,
        stores_prefetch=stores_prefetch, prefetches=prefetches,
        coh_upgrades=coh_upgrades
    )

def read_llc_latency_from_db(run_dir):
    """
    Return L3 latency/time buckets (ns) over ROI (end - begin):
      l3_total_latency_ns, l3_mshr_latency_ns, l3_snoop_latency_ns, l3_qbs_latency_ns
      l3_uncore_time_sum_ns = sum of uncore-time-* (safe)
    """
    db = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db):
        return {}
    with sqlite3.connect(db) as conn:
        cur = conn.cursor()

        def one_delta_real(metricname):
            cur.execute("""
                SELECT COALESCE(SUM(
                    CASE p.prefixname
                        WHEN 'roi-end'   THEN v.value*1.0
                        WHEN 'roi-begin' THEN -v.value*1.0
                        ELSE 0
                    END
                ), 0.0)
                FROM "values" v
                JOIN names    n ON v.nameid   = n.nameid
                JOIN prefixes p ON v.prefixid = p.prefixid
                WHERE n.objectname='L3'
                  AND n.metricname = ?
                  AND p.prefixname IN ('roi-begin','roi-end');
            """, (metricname,))
            row = cur.fetchone()
            return float(row[0] or 0.0)

        lat = {
            "l3_total_latency_ns":  one_delta_real("total-latency"),
            "l3_mshr_latency_ns":   one_delta_real("mshr-latency"),
            "l3_snoop_latency_ns":  one_delta_real("snoop-latency"),
            "l3_qbs_latency_ns":    one_delta_real("qbs-query-latency"),
        }

        # Sum all uncore-time-* over ROI
        cur.execute("""
            SELECT COALESCE(SUM(
                CASE p.prefixname
                    WHEN 'roi-end'   THEN v.value*1.0
                    WHEN 'roi-begin' THEN -v.value*1.0
                    ELSE 0
                END
            ), 0.0)
            FROM "values" v
            JOIN names    n ON v.nameid   = n.nameid
            JOIN prefixes p ON v.prefixid = p.prefixid
            WHERE n.objectname='L3'
              AND n.metricname LIKE 'uncore-time-%'
              AND p.prefixname IN ('roi-begin','roi-end');
        """)
        lat["l3_uncore_time_sum_ns"] = float(cur.fetchone()[0] or 0.0)

    return lat

def read_uncore_requests(run_dir):
    db = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db):
        return 0
    with sqlite3.connect(db) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT COALESCE(SUM(
                CASE p.prefixname
                    WHEN 'roi-end'   THEN v.value
                    WHEN 'roi-begin' THEN -v.value
                    ELSE 0
                END
            ), 0)
            FROM "values" v
            JOIN names    n ON v.nameid   = n.nameid
            JOIN prefixes p ON v.prefixid = p.prefixid
            WHERE n.objectname='L3'
              AND n.metricname='uncore-requests'
              AND p.prefixname IN ('roi-begin','roi-end');
        """)
        row = cur.fetchone()
        return int(row[0] or 0)

def parse_llc_hit_cycles(run_dir):
    """
    Return effective LLC read/write hit cycles.

    Reads:
      sim.cfg:
        [perf_model/l3_cache/llc] and optional [perf_model/l3_cache/llc.next_read]
        or fully-qualified perf_model/... keys.
      sim.stats.sqlite3 (optional) to weight local vs remote hits over ROI.

    Returns: (rd_cycles:int|None, wr_cycles:int|None)
    """
    cfg = os.path.join(run_dir, "sim.cfg")
    rd_llc = wr_llc = None
    rd_next = wr_next = None
    sect = None

    # Parse sim.cfg
    if os.path.exists(cfg):
        try:
            with open(cfg, "r", encoding="utf-8", errors="ignore") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith(("#",";")):
                        continue
                    m = re.match(r'\[(.+?)\]', line)
                    if m:
                        sect = m.group(1).strip().lower()
                        continue

                    m = re.match(r'perf_model/l3_cache/llc(?:\.next_read)?/read_hit_latency_cycles\s*=\s*([0-9]+)', line, flags=re.I)
                    if m:
                        val = int(m.group(1))
                        if ".next_read/" in line.lower():
                            rd_next = val
                        else:
                            rd_llc = val
                        continue

                    m = re.match(r'perf_model/l3_cache/llc(?:\.next_read)?/write_hit_latency_cycles\s*=\s*([0-9]+)', line, flags=re.I)
                    if m:
                        val = int(m.group(1))
                        if ".next_read/" in line.lower():
                            wr_next = val
                        else:
                            wr_llc = val
                        continue

                    if sect in ("perf_model/l3_cache/llc", "perf_model/l3_cache/llc.next_read"):
                        m = re.match(r'read_hit_latency_cycles\s*=\s*([0-9]+)', line, flags=re.I)
                        if m:
                            val = int(m.group(1))
                            if sect.endswith(".next_read"):
                                rd_next = val
                            else:
                                rd_llc = val
                            continue
                        m = re.match(r'write_hit_latency_cycles\s*=\s*([0-9]+)', line, flags=re.I)
                        if m:
                            val = int(m.group(1))
                            if sect.endswith(".next_read"):
                                wr_next = val
                            else:
                                wr_llc = val
                            continue
        except Exception:
            pass

    have_llc  = (rd_llc is not None and wr_llc is not None)
    have_next = (rd_next is not None and wr_next is not None)

    if not have_llc and not have_next:
        return None, None
    if have_llc and not have_next:
        return rd_llc, wr_llc
    if have_next and not have_llc:
        return rd_next, wr_next

    # Weight by ROI-delta local vs remote hits (if DB is present)
    db = os.path.join(run_dir, "sim.stats.sqlite3")
    if not os.path.exists(db):
        return rd_llc, wr_llc

    def _roi_delta(cur, metric, obj_exact=None, obj_like=None):
        if obj_exact is not None:
            cur.execute("""
                SELECT COALESCE(SUM(
                    CASE p.prefixname
                        WHEN 'roi-end'   THEN v.value
                        WHEN 'roi-begin' THEN -v.value
                        ELSE 0
                    END
                ), 0)
                FROM "values" v
                JOIN names    n ON v.nameid   = n.nameid
                JOIN prefixes p ON v.prefixid = p.prefixid
                WHERE n.metricname=? AND n.objectname=? AND p.prefixname IN ('roi-begin','roi-end');
            """, (metric, obj_exact))
        else:
            cur.execute("""
                SELECT COALESCE(SUM(
                    CASE p.prefixname
                        WHEN 'roi-end'   THEN v.value
                        WHEN 'roi-begin' THEN -v.value
                        ELSE 0
                    END
                ), 0)
                FROM "values" v
                JOIN names    n ON v.nameid   = n.nameid
                JOIN prefixes p ON v.prefixid = p.prefixid
                WHERE n.metricname=? AND n.objectname LIKE ? AND p.prefixname IN ('roi-begin','roi-end');
            """, (metric, obj_like))
        row = cur.fetchone()
        try:
            return int(row[0] or 0)
        except Exception:
            return int(float(row[0] or 0.0))

    try:
        with sqlite3.connect(db) as conn:
            cur = conn.cursor()
            RH_local = _roi_delta(cur, "l3_read_hits",  obj_exact="L3")
            RH_all   = _roi_delta(cur, "l3_read_hits",  obj_like="L3%")
            WH_local = _roi_delta(cur, "l3_write_hits", obj_exact="L3")
            WH_all   = _roi_delta(cur, "l3_write_hits", obj_like="L3%")
    except Exception:
        return rd_llc, wr_llc

    RH_remote = max(RH_all - RH_local, 0)
    WH_remote = max(WH_all - WH_local, 0)

    rd_total = RH_local + RH_remote
    wr_total = WH_local + WH_remote

    rd_eff = rd_llc if rd_total == 0 else int(round((RH_local*rd_llc + RH_remote*(rd_next if rd_next is not None else rd_llc)) / float(rd_total)))
    wr_eff = wr_llc if wr_total == 0 else int(round((WH_local*wr_llc + WH_remote*(wr_next if wr_next is not None else wr_llc)) / float(wr_total)))

    return rd_eff, wr_eff

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

    cfg = os.path.join(run_dir, "sim.cfg")
    if os.path.exists(cfg):
        try:
            with open(cfg, "r", encoding="utf-8", errors="ignore") as f:
                vals = _scan_text(f.read())
                if vals: return vals
        except Exception:
            pass

    info = os.path.join(run_dir, "sim.info")
    if os.path.exists(info):
        try:
            with open(info, "r", encoding="utf-8", errors="ignore") as f:
                vals = _scan_text(f.read())
                if vals: return vals
        except Exception:
            pass

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
# Reconcile missing hits (coherency upgrades etc.) for energy
# =========================
def reconcile_hits_for_energy(db):
    """
    Ensure RH+WH == (A_db - M_db) by allocating the missing hits (gap)
    primarily to writes (coherency upgrades), then split any remainder
    proportionally by the observed RH:WH ratio (or 50/50 if none).
    Returns: RH_use, WH_use, gap
    """
    if not db:
        return 0, 0, 0
    H_tot = max((db["A_db"] - db["M_db"]), 0)
    RHc, WHc = db["RH"] or 0, db["WH"] or 0
    counted = RHc + WHc
    gap = H_tot - counted
    if gap <= 0:
        return RHc, WHc, 0

    # allocate as write-like hits up to coh_upgrades
    add_w = min(gap, db.get("coh_upgrades") or 0)
    RH_use, WH_use = RHc, WHc + add_w
    rem = gap - add_w
    if rem > 0:
        denom = counted if counted > 0 else 2
        r = (RHc / denom) if counted > 0 else 0.5
        RH_use += int(round(rem * r))
        WH_use  = H_tot - RH_use
    return RH_use, WH_use, gap

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

    # Rebalance hits for energy use (accounts for coherency upgrades)
    s_RH_use, s_WH_use, s_gap = reconcile_hits_for_energy(s_db) if s_db else (0, 0, 0)
    n_RH_use, n_WH_use, n_gap = reconcile_hits_for_energy(n_db) if n_db else (0, 0, 0)

    # Avg hit ns based on effective cycles and rebalanced hit mix
    s_avg_hit_ns = avg_l3_hit_ns(s_rd_cyc, s_wr_cyc, s_RH_use, s_WH_use, s_period_ns)
    n_avg_hit_ns = avg_l3_hit_ns(n_rd_cyc, n_wr_cyc, n_RH_use, n_WH_use, n_period_ns)

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

    # Exact energies (using rebalanced hits)
    s_dyn_exact = s_E_exact = float('nan')
    n_dyn_exact = n_E_exact = float('nan')
    s_exact_src = n_exact_src = ""
    s_leakW = s_consts["P_leak_W"]
    n_leakW = n_consts["P_leak_W"]

    def mismatch_note(d):
        if d is None:
            return "sqlite_missing"
        other = (d["A_db"] - d["M_db"]) - (d["RH"] + d["WH"])
        if -2 <= other <= 2:  # tolerate ±2 boundary jitter
            return "ok"
        up = d.get("coh_upgrades", 0)
        hp = d.get("hits_prefetch", 0)
        extras = []
        if up: extras.append(f"upgrades={up}")
        if hp: extras.append(f"hits_prefetch={hp}")
        suffix = ("; " + ", ".join(extras)) if extras else ""
        return f"warn_A-M!=RH+WH(diff={other}{suffix})"

    s_note = mismatch_note(s_db)
    n_note = mismatch_note(n_db)

    if s_db is not None:
        s_dyn_exact, s_E_exact = energy_exact_from_counts(
            s_consts["E_hit_nJ"], s_consts["E_miss_nJ"], s_consts["E_write_nJ"], s_leakW, T_s,
            s_RH_use, s_WH_use, s_db["M_db"]
        )
        s_exact_src = "sqlite" + ("+rebalanced" if s_gap > 0 else "")

    if n_db is not None:
        n_dyn_exact, n_E_exact = energy_exact_from_counts(
            n_consts["E_hit_nJ"], n_consts["E_miss_nJ"], n_consts["E_write_nJ"], n_leakW, T_n,
            n_RH_use, n_WH_use, n_db["M_db"]
        )
        n_exact_src = "sqlite" + ("+rebalanced" if n_gap > 0 else "")

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

