#!/usr/bin/env python3
import argparse, re, math, os, gzip
from collections import Counter

LINE_COLON = re.compile(r'^\s*(0x[0-9a-fA-F]+)\s*:\s*(\d+)\s*,\s*([A-Za-z]+)')
VIEW_RW_COMMA = re.compile(r'.*?,\s*([RrWw])\s*,\s*(\d+)\s*,\s*(0x[0-9a-fA-F]+)')
VIEW_RW_SPACE = re.compile(r'^\s*\d+\s+\d+:\s+\d+\s+(read|write)\s+(\d+)\s+byte\(s\)\s+@\s+(0x[0-9a-fA-F]+)')

def parse_kind_addr(line):
    # 1) "0xADDR: SIZE, KIND"
    m = LINE_COLON.match(line)
    if m:
        addr = int(m.group(1), 16)
        k = m.group(3).lower()
        if k.startswith('r'):  return 'R', addr
        if k.startswith('w'):  return 'W', addr
        return None, None

    # 2) CSV "pc,r/w,size,addr"
    parts = [p.strip() for p in line.split(',')]
    if len(parts) >= 4:
        k = parts[1].lower()
        try:
            if k.startswith('r') or k == 'load':  return 'R', int(parts[3], 16)
            if k.startswith('w') or k == 'store': return 'W', int(parts[3], 16)
        except Exception:
            pass

    # 3) drcachesim view (comma form)
    m = VIEW_RW_COMMA.match(line)
    if m:
        kind = 'R' if m.group(1).lower().startswith('r') else 'W'
        addr = int(m.group(3), 16)
        return kind, addr

    # 4) drcachesim view (space form like: "write 8 byte(s) @ 0xADDR ...")
    m = VIEW_RW_SPACE.match(line)
    if m:
        kind = 'R' if m.group(1).lower() == 'read' else 'W'
        addr = int(m.group(3), 16)
        return kind, addr

    return None, None

def entropy(counter):
    t = sum(counter.values())
    if t == 0: return 0.0
    H = 0.0
    for c in counter.values():
        p = c / t
        H -= p * math.log2(p)
    return H

def footprint90(counter):
    t = sum(counter.values())
    if t == 0: return 0
    need = 0.9 * t
    acc = 0; cnt = 0
    for _, c in counter.most_common():
        acc += c; cnt += 1
        if acc >= need: break
    return cnt

def parse_kind_addr(line):
    # Supports both "0xADDR: SIZE, KIND" and "pc,r/w,size,addr"
    m = LINE_COLON.match(line)
    if m:
        addr = int(m.group(1), 16)
        k = m.group(3).lower()
        if k in ('r','read','load','ld','mem-read'):  return 'R', addr
        if k in ('w','write','store','st','mem-write'): return 'W', addr
        return None, None
    parts = [p.strip() for p in line.split(',')]
    if len(parts) >= 4:
        k = parts[1].lower()
        try:
            addr = int(parts[3], 16)
        except Exception:
            return None, None
        if k.startswith('r'): return 'R', addr
        if k.startswith('w'): return 'W', addr
    return None, None

# scripts/mem_metrics_unit.py
import os, gzip
def open_any(path):
    if path.endswith('.gz'):
        return gzip.open(path, 'rt', errors='ignore')
    plain_exists = os.path.exists(path)
    gz_exists    = os.path.exists(path + '.gz')
    plain_is_empty = plain_exists and os.path.getsize(path) == 0
    if plain_exists and not plain_is_empty:
        return open(path, 'r', errors='ignore')
    if gz_exists:
        return gzip.open(path + '.gz', 'rt', errors='ignore')
    raise FileNotFoundError(path)

def compute_metrics(paths, M, unit_shift, exclude_stack):
    R = Counter(); W = Counter(); Rloc = Counter(); Wloc = Counter()
    if isinstance(paths, str): paths = [paths]
    for p in paths:
        with open_any(p) as f:
            for line in f:
                if not line.strip() or line.startswith('Format:'): continue
                k, addr = parse_kind_addr(line)
                if not k: continue
                if exclude_stack and (0x00007fff00000000 <= addr < 0x0000800000000000):
                    # crude "likely user stack" filter in Linux user VA space
                    continue
                key = addr >> unit_shift  # 0: byte, 6: line(64B), 12: page(4KiB)
                if k == 'R':
                    R[key] += 1;  Rloc[addr >> M] += 1
                else:
                    W[key] += 1;  Wloc[addr >> M] += 1
    return {
        "read_total": sum(R.values()), "read_unique": len(R),
        "read_entropy": entropy(R), "read_local_entropy": entropy(Rloc),
        "read_footprint90": footprint90(R),
        "write_total": sum(W.values()), "write_unique": len(W),
        "write_entropy": entropy(W), "write_local_entropy": entropy(Wloc),
        "write_footprint90": footprint90(W)
    }

def append_csv(csv_path, name, M, m):
    new = not os.path.exists(csv_path)
    with open(csv_path, 'a') as f:
        if new:
            f.write(",".join([
                "name","M",
                "read_total","read_unique","read_entropy","read_local_entropy","read_footprint90",
                "write_total","write_unique","write_entropy","write_local_entropy","write_footprint90"
            ]) + "\n")
        f.write(",".join(map(str, [
            name, M,
            m["read_total"], m["read_unique"], f"{m['read_entropy']:.6f}", f"{m['read_local_entropy']:.6f}", m["read_footprint90"],
            m["write_total"], m["write_unique"], f"{m['write_entropy']:.6f}", f"{m['write_local_entropy']:.6f}", m["write_footprint90"],
        ])) + "\n")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="trace")
    ap.add_argument("--csv", default="features.csv")
    ap.add_argument("--M", type=int, default=10, help="bits for local entropy bucketing (6≈64B, 12≈4KiB)")
    ap.add_argument("--unit", choices=["byte","line","page"], default="byte")
    ap.add_argument("--exclude-stack", action="store_true", help="filter likely user stack addresses (0x7fff...)")
    ap.add_argument("tracefiles", nargs='+')
    args = ap.parse_args()
    unit_shift = {"byte":0, "line":6, "page":12}[args.unit]
    m = compute_metrics(args.tracefiles, args.M, unit_shift, args.exclude_stack)
    append_csv(args.csv, args.name, args.M, m)
    print(m)
