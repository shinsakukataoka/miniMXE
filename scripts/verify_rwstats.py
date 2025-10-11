#!/usr/bin/env python3
import argparse, os, re, math, sys

# ---------- parsing ----------
NUM_RE = re.compile(
    r'([A-Za-z0-9_]+)='
    r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|nan|NaN|inf|Inf|Infinity)'
)

def parse_scope_lines(path, n=2):
    """Return up to the last n scope= lines parsed into dicts (with '_raw')."""
    lines = []
    try:
        with open(path, 'r', errors='ignore') as f:
            for line in f:
                s = line.strip()
                if s.startswith('scope=') and '=' in s:
                    lines.append(s)
    except OSError as e:
        return []

    selected = lines[-n:] if lines else []
    out = []
    for s in selected:
        d = {}
        for k, v in NUM_RE.findall(s):
            vl = v.lower()
            if vl == 'nan':
                d[k] = math.nan
            elif vl in ('inf', 'infinity'):
                d[k] = math.inf
            else:
                try:
                    d[k] = float(v)
                except ValueError:
                    pass
        d['_raw'] = s
        out.append(d)
    return out

# ---------- helpers ----------
def almost_eq(a, b, rel=0.01, abs_tol=1.0):
    if any(math.isnan(x) for x in (a, b)):
        return None
    return abs(a - b) <= max(abs_tol, rel * max(abs(a), abs(b)))

def check_cap(stats):
    sboc = stats.get('stride_bytes_over_cap', 0.0)
    sloc = stats.get('line_stride_over_cap', 0.0)
    sboc_hit = (sboc is not None) and (not math.isnan(sboc)) and sboc > 0
    sloc_hit = (sloc is not None) and (not math.isnan(sloc)) and sloc > 0
    return sboc_hit or sloc_hit

def summarize(checks):
    ok = sum(1 for _, c, _ in checks if c is True)
    bad = sum(1 for _, c, _ in checks if c is False)
    na  = sum(1 for _, c, _ in checks if c is None)
    return ok, bad, na

# ---------- invariants ----------
def check_percentiles(stats, line_bytes):
    """Cap-aware pXX_strideB ≈ pXX_strideL*line_bytes checks."""
    items = []
    capped = check_cap(stats)

    # best-effort cap sentinels from the observed percentiles (no env reliance)
    obsB = [stats.get(k) for k in ('p50_strideB','p90_strideB','p99_strideB')]
    obsL = [stats.get(k) for k in ('p50_strideL','p90_strideL','p99_strideL')]
    maxB = max([x for x in obsB if x is not None and not math.isnan(x)], default=None)
    maxL = max([x for x in obsL if x is not None and not math.isnan(x)], default=None)

    for p in ('50','90','99'):
        kb = f"p{p}_strideB"
        kl = f"p{p}_strideL"
        if kb in stats and kl in stats:
            B = stats[kb]; L = stats[kl]
            if any(x is None or math.isnan(x) for x in (B, L)):
                items.append((f"{kb} == {kl}*{int(line_bytes)}", None, "NaN/missing"))
                continue
            expectB = L * line_bytes
            ok = almost_eq(B, expectB, rel=0.01, abs_tol=1.0)
            if ok:
                items.append((f"{kb} == {kl}*{int(line_bytes)}", True, f"{B} ≈ {expectB}"))
            elif capped or (maxB is not None and B == maxB) or (maxL is not None and L == maxL):
                items.append((f"{kb} == {kl}*{int(line_bytes)}", None,
                              f"cap-hit: B={B}, L={L}, expect={expectB}"))
            else:
                items.append((f"{kb} == {kl}*{int(line_bytes)}", False,
                              f"{B} vs {expectB}"))
        else:
            items.append((f"{kb} == {kl}*{int(line_bytes)}", None, "missing keys"))
    return items

def check_invariants(last, prev, line_bytes):
    checks = []

    # 1) footprint_bytes ~= uniq_lines * line_bytes
    if 'footprint_bytes' in last and 'uniq_lines' in last:
        ok = almost_eq(last['footprint_bytes'], last['uniq_lines'] * line_bytes,
                       rel=0.0, abs_tol=line_bytes)
        checks.append(("footprint == uniq_lines*line_bytes",
                       ok,
                       f"{last.get('footprint_bytes')} vs {last.get('uniq_lines')}*{line_bytes}"))
    else:
        checks.append(("footprint == uniq_lines*line_bytes", None, "missing keys"))

    # 2) percentile bytes vs lines (cap-aware)
    checks.extend(check_percentiles(last, line_bytes))

    # 3) reuse_rate ≈ 1 - read_unique/read_total (interval)
    if all(k in last for k in ('reuse_rate','read_total','read_unique')):
        rr, tot, uni = last['reuse_rate'], last['read_total'], last['read_unique']
        if not math.isnan(rr) and tot > 0 and not math.isnan(uni):
            approx = 1.0 - (uni / tot)
            checks.append(("reuse_rate ≈ 1 - read_unique/read_total",
                           abs(rr - approx) <= 0.03,
                           f"{rr} vs {approx:.6f}"))
        else:
            checks.append(("reuse_rate ≈ 1 - read_unique/read_total", None, "n/a"))
    else:
        checks.append(("reuse_rate ≈ 1 - read_unique/read_total", None, "missing"))

    # 4) 90% coverage in lines ≤ corresponding global unique-line counters
    for kind in ('read','write'):
        f90 = f"{kind}_footprint90L"
        uul = f"{kind}_unique_lines"
        if f90 in last and uul in last:
            f90v = last[f90]; uulv = last[uul]
            if any(math.isnan(x) for x in (f90v, uulv)):
                checks.append((f"{f90} ≤ {uul}", None, "NaN"))
            else:
                checks.append((f"{f90} ≤ {uul}", f90v <= uulv, f"{f90v} ≤ {uulv}"))
        else:
            checks.append((f"{f90} ≤ {uul}", None, "missing"))

    # 5) basic non-negatives
    for k in ('reads','writes','bytes_read','bytes_written','uniq_lines','uniq_pages'):
        v = last.get(k, None)
        if v is None or math.isnan(v):
            checks.append((f"{k} ≥ 0", None, "missing"))
        else:
            checks.append((f"{k} ≥ 0", v >= 0, f"{k}={v}"))

    # 6) normalized ranges
    for k, lo, hi in (('reuse_rate',0.0,1.0), ('p_stride_le_64',0.0,1.0)):
        v = last.get(k, None)
        if v is None or math.isnan(v):
            checks.append((f"{k} in [{lo},{hi}]", None, f"{k}={v}"))
        else:
            checks.append((f"{k} in [{lo},{hi}]", lo <= v <= hi, f"{k}={v}"))

    # 7) if we have the previous scope line, do monotonic + delta checks
    if prev:
        for k in ('reads','writes','global_footprint_bytes','read_unique_lines','write_unique_lines'):
            a, b = prev.get(k), last.get(k)
            if a is None or b is None or any(math.isnan(x) for x in (a, b)):
                checks.append((f"{k} monotonic (prev→last)", None, "missing"))
            else:
                checks.append((f"{k} monotonic (prev→last)", b >= a, f"{a}→{b}"))

        # Δreads ≈ read_total ; Δwrites ≈ write_total
        if all(k in last and k in prev for k in ('reads','read_total','writes','write_total')):
            if not any(math.isnan(x) for x in (last['reads'], prev['reads'], last['read_total'])):
                dR = last['reads'] - prev['reads']
                ok = almost_eq(dR, last['read_total'], rel=0.02, abs_tol=5.0)
                checks.append(("Δreads ≈ read_total", ok, f"Δ={dR} vs {last['read_total']}"))
            else:
                checks.append(("Δreads ≈ read_total", None, "NaN"))
            if not any(math.isnan(x) for x in (last['writes'], prev['writes'], last['write_total'])):
                dW = last['writes'] - prev['writes']
                ok = almost_eq(dW, last['write_total'], rel=0.02, abs_tol=5.0)
                checks.append(("Δwrites ≈ write_total", ok, f"Δ={dW} vs {last['write_total']}"))
            else:
                checks.append(("Δwrites ≈ write_total", None, "NaN"))
        else:
            checks.append(("Δreads ≈ read_total", None, "need two scope lines"))
            checks.append(("Δwrites ≈ write_total", None, "need two scope lines"))
    else:
        for k in ('reads','writes','global_footprint_bytes','read_unique_lines','write_unique_lines'):
            checks.append((f"{k} monotonic (prev→last)", None, "need ≥2 scope lines"))
        checks.append(("Δreads ≈ read_total", None, "need ≥2 scope lines"))
        checks.append(("Δwrites ≈ write_total", None, "need ≥2 scope lines"))

    return checks

# ---------- main ----------
def main():
    ap = argparse.ArgumentParser(description="Cap-aware verifier for DynamoRIO rwstats interval logs (prints only).")
    ap.add_argument("--logs", default="/home/skataoka26/COSC_498/miniMXE/results_trace/logs",
                    help="Directory with rwstats logs")
    ap.add_argument("--ts", required=True,
                    help="Timestamp prefix, e.g. 20250929T203551Z")
    ap.add_argument("--line-bytes", type=float, default=64.0,
                    help="Cache line size in bytes (default 64)")
    ap.add_argument("--show-raw", action="store_true",
                    help="Print the full last scope= line (not just a prefix)")
    ap.add_argument("--max-files", type=int, default=0,
                    help="Optional limit on number of files to process (0 = all)")
    args = ap.parse_args()

    if not os.path.isdir(args.logs):
        print(f"[ERR] logs dir not found: {args.logs}", file=sys.stderr)
        sys.exit(2)

    files = [f for f in os.listdir(args.logs)
             if f.startswith(args.ts) and f.endswith("_instr.rwstats.log")]
    files.sort()
    if args.max_files > 0:
        files = files[:args.max_files]

    if not files:
        print(f"[WARN] no logs matching prefix {args.ts} in {args.logs}")
        sys.exit(0)

    total_ok = total_bad = total_na = 0
    print(f"=== Verifying logs in {args.logs} (prefix={args.ts}) ===")

    for fn in files:
        path = os.path.join(args.logs, fn)
        scopes = parse_scope_lines(path, n=2)
        if not scopes:
            print(f"\n[{fn}]\n  [WARN] no scope= lines found")
            continue

        last = scopes[-1]
        prev = scopes[-2] if len(scopes) >= 2 else None

        print(f"\n[{fn}]")
        raw = last.get('_raw', '')
        print("  last:", raw if args.show_raw else (raw[:120] + ("..." if len(raw) > 120 else "")))

        checks = check_invariants(last, prev=prev, line_bytes=args.line_bytes)
        ok, bad, na = summarize(checks)
        total_ok += ok; total_bad += bad; total_na += na

        for name, passed, note in checks:
            tag = "PASS" if passed is True else ("FAIL" if passed is False else "n/a ")
            print(f"   - {tag:4} {name:42s} ({note})")
        print(f"  -> file summary: PASS={ok} FAIL={bad} N/A={na}")

    print("\n=== Overall Summary ===")
    print(f"  PASS={total_ok}  FAIL={total_bad}  N/A={total_na}")
    if total_bad == 0:
        print("  ✔ All invariants passed where applicable.")
    else:
        print("  ✖ Some checks failed. Inspect details above.")

if __name__ == "__main__":
    main()

