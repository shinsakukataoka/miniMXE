#!/usr/bin/env python3
import sys, re, math, argparse, os, csv

def parse_simout(path: str):
    try:
        t = open(path).read()
    except Exception as e:
        print(f"[WARN] Cannot read {path}: {e}")
        return math.nan, 0, 0
    m = re.search(r'Time \(ns\)\s*\|\s*([0-9.]+)', t)
    T_s = float(m.group(1))/1e9 if m else float('nan')
    blk = re.search(r'Cache L3.*?num cache accesses\s*\|\s*([0-9]+).*?num cache misses\s*\|\s*([0-9]+)', t, re.S)
    acc = int(blk.group(1)) if blk else 0
    mis = int(blk.group(2)) if blk else 0
    return T_s, acc, mis

def energy_bounds(E_hit, E_miss, E_write, P_leak, T_s, acc, mis):
    hits = max(acc - mis, 0)
    Edyn_lo_nJ = E_hit*hits + E_miss*mis     # all hits are reads
    Edyn_hi_nJ = E_write*hits + E_miss*mis   # all hits are writes
    Eleak_J = P_leak * T_s
    Etot_lo_J = Edyn_lo_nJ*1e-9 + Eleak_J
    Etot_hi_J = Edyn_hi_nJ*1e-9 + Eleak_J
    return (Etot_lo_J, Etot_hi_J), (Edyn_lo_nJ, Edyn_hi_nJ), Eleak_J

def ed2p(EJ, T):
    return EJ * (T**2)

parser = argparse.ArgumentParser()
parser.add_argument('sram_simout')
parser.add_argument('jans_simout')
parser.add_argument('-o','--out-csv', default=None, help='optional CSV to append results')
args = parser.parse_args()

T_s, A_s, M_s = parse_simout(args.sram_simout)
T_n, A_n, M_n = parse_simout(args.jans_simout)

# Energy model constants (LLC only)
SRAM = dict(E_hit=0.565, E_miss=0.011, E_write=0.537, P_leak=3.438)
JANS = dict(E_hit=0.188, E_miss=0.077, E_write=2.305, P_leak=0.048)

(Elo_s, Ehi_s), (_, _), Sleak = energy_bounds(**SRAM, T_s=T_s, acc=A_s, mis=M_s)
(Elo_n, Ehi_n), (_, _), Nleak = energy_bounds(**JANS, T_s=T_n, acc=A_n, mis=M_n)

print("\n==== Post-run energy/ED^2P (LLC only, bounds) ====")
print(f"SRAM: T_sim={T_s:.6f}s, L3_accesses={A_s}, L3_misses={M_s}")
print(f"JanS: T_sim={T_n:.6f}s, L3_accesses={A_n}, L3_misses={M_n}")

print("\nLLC Energy (J): lower .. upper (includes leakage)")
print(f"  SRAM : {Elo_s:.6f} .. {Ehi_s:.6f}   [leak={Sleak:.6f} J]")
print(f"  JanS : {Elo_n:.6f} .. {Ehi_n:.6f}   [leak={Nleak:.6f} J]")

print("\nED^2P (J*s^2): lower .. upper")
print(f"  SRAM : {ed2p(Elo_s,T_s):.9e} .. {ed2p(Ehi_s,T_s):.9e}")
print(f"  JanS : {ed2p(Elo_n,T_n):.9e} .. {ed2p(Ehi_n,T_n):.9e}")

if (T_s > 0 and T_n > 0):
    print(f"\nSpeedup (SRAM/JanS): {T_s/T_n:.3f}x")

if args.out_csv:
    hdr = ["bench","n_m","metric","sram_lo","sram_hi","jans_lo","jans_hi"]
    row_ed2p  = [os.getenv('BENCH',''), os.getenv('N_M',''), "ED2P",
                 f"{ed2p(Elo_s,T_s):.6e}", f"{ed2p(Ehi_s,T_s):.6e}",
                 f"{ed2p(Elo_n,T_n):.6e}", f"{ed2p(Ehi_n,T_n):.6e}"]
    row_energy = [os.getenv('BENCH',''), os.getenv('N_M',''), "EnergyJ",
                 f"{Elo_s:.6f}", f"{Ehi_s:.6f}", f"{Elo_n:.6f}", f"{Ehi_n:.6f}"]
    write_hdr = not os.path.exists(args.out_csv)
    with open(args.out_csv,'a', newline='') as f:
        w = csv.writer(f)
        if write_hdr: w.writerow(hdr)
        w.writerow(row_energy)
        w.writerow(row_ed2p)

