#!/usr/bin/env bash
#SBATCH --job-name=spec-features
#SBATCH --partition=cpu-q
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=02:00:00
#SBATCH --output=%x-%j.out
#SBATCH --error=%x-%j.err
#SBATCH --chdir=/home/skataoka26/COSC_498/miniMXE   # <— ensure correct working dir

set -euo pipefail

# where DynamoRIO is
export DR_HOME=${DR_HOME:-$HOME/opt/DynamoRIO-Linux-11.3.0-1}

# fresh CSV each run, under data/
mkdir -p "$PWD/data"
OUT_CSV="$PWD/data/features.csv"
rm -f "$OUT_CSV"

shopt -s nullglob
for f in results_trace/traces/*_5s.log.gz; do
  base="${f##*/}"                      # e.g., 2025...Z_541_leela_r_5s.log.gz
  bench_u="${base#*_}"; bench_u="${bench_u%_5s.log.gz}"
  bench="${bench_u/_/.}"               # 541.leela_r
  echo ">>> features for $bench"

  # Use the same invocation you had before (process substitution)
  /usr/bin/timeout 600s python3 scripts/mem_metrics_v3.py \
  --name "$bench" --csv "$OUT_CSV" --M 10 <(gzip -cd "$f") \
    || echo "[WARN] features failed for $bench (rc=$?)"
done

# summary
[[ -f "$OUT_CSV" ]] && echo "done. wrote $(wc -l < "$OUT_CSV") lines to $OUT_CSV (incl. header)" || \
  echo "[WARN] no rows written (no matching traces?)"

