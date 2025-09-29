#!/usr/bin/env python3
import argparse, re, math, os
from collections import Counter

def entropy(counter):
    total = sum(counter.values())
    if total == 0: return 0.0
    H = 0.0
    for c in counter.values():
        p = c / total
        H -= p * math.log2(p)
    return H

def footprint90(counter):
    total = sum(counter.values())
    if total == 0: return 0
    need = 0.9 * total
    acc = 0; cnt = 0
    for _, c in counter.most_common():
        acc += c; cnt += 1
        if acc >= need: break
    return cnt

# regex for "0xADDR: SIZE, KIND" style
LINE_COLON = re.compile(r'^\s*(0x[0-9a-fA-F]+)\s*:\s*(\d+)\s*,\s*([A-Za-z]+)')

def compute_metrics(path, M):
    R = Counter(); W = Counter(); Rloc = Counter(); Wloc = Counter()
    with open(path, 'r', errors='ignore') as f:
        for line in f:
            if not line.strip() or line.startswith('Format:'):  # skip headers/blanks
                continue
            kind = None
            addr = None

            # Try "0xADDR: SIZE, KIND" (memtrace_simple, older text)
            m = LINE_COLON.match(line)
            if m:
                addr = int(m.group(1), 16)
                k = m.group(3).lower()
                if k in ('r','read','load','ld','mem-read'):
                    kind = 'R'
                elif k in ('w','write','store','st','mem-write'):
                    kind = 'W'
                else:
                    kind = None  # opcode like mov/push/etc: ignore
            else:
                # Try CSV-ish "instr_addr,r/w,size,data_addr" (your deepsjeng log)
                parts = [p.strip() for p in line.split(',')]
                if len(parts) >= 4:
                    k = parts[1].lower()
                    if k.startswith('r'): kind = 'R'
                    elif k.startswith('w'): kind = 'W'
                    if kind:
                        # data addr is the 4th field
                        try:
                            addr = int(parts[3], 16)
                        except ValueError:
                            addr = None

            if kind and addr is not None:
                if kind == 'R':
                    R[addr] += 1
                    Rloc[addr >> M] += 1
                else:
                    W[addr] += 1
                    Wloc[addr >> M] += 1

    return {
        "read_total": sum(R.values()),
        "read_unique": len(R),
        "read_entropy": entropy(R),
        "read_local_entropy": entropy(Rloc),
        "read_footprint90": footprint90(R),
        "write_total": sum(W.values()),
        "write_unique": len(W),
        "write_entropy": entropy(W),
        "write_local_entropy": entropy(Wloc),
        "write_footprint90": footprint90(W),
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
    ap.add_argument("tracefile")
    ap.add_argument("--name", default=None)
    ap.add_argument("--csv", default="features.csv")
    ap.add_argument("--M", type=int, default=10, help="bits to drop for 'local' entropy (10 ~ 1KB; 12 ~ 4KB)")
    args = ap.parse_args()
    name = args.name or os.path.basename(args.tracefile)
    m = compute_metrics(args.tracefile, args.M)
    append_csv(args.csv, name, args.M, m)
    print(f"READ : total={m['read_total']:,}  unique={m['read_unique']:,}  "
          f"entropy={m['read_entropy']:.3f}  local_entropy(M={args.M})={m['read_local_entropy']:.3f}  "
          f"footprint90={m['read_footprint90']:,}")
    print(f"WRITE: total={m['write_total']:,}  unique={m['write_unique']:,}  "
          f"entropy={m['write_entropy']:.3f}  local_entropy(M={args.M})={m['write_local_entropy']:.3f}  "
          f"footprint90={m['write_footprint90']:,}")

