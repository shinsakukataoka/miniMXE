#!/bin/bash
#SBATCH -J feat-only
#SBATCH -p cpu-q
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH -o feat-only-%j.out
#SBATCH -e feat-only-%j.err

set -Eeuo pipefail


OUT_ROOT="$HOME/COSC_498/miniMXE/results_trace"
SCRIPT="$HOME/COSC_498/miniMXE/scripts/mem_metrics_unit.py"

for v in "$OUT_ROOT"/traces/drmem.*.view.log.gz; do
  # skip corrupted/in-flight gz
  gzip -t "$v" >/dev/null 2>&1 || { echo "[skip-bad] $v"; continue; }
  # skip logs with no memrefs
  zgrep -qE ' (read|write) +[0-9]+ byte\(s\) @ 0x' "$v" || { echo "[skip-empty] $v"; continue; }

  bench=$(basename "$v" | cut -d. -f2)

  # IMPORTANT: pass *the gz path itself* (not ${v%.gz})
  python3 "$SCRIPT" --unit line --exclude-stack --M 6 \
    --name "$bench" --csv "$OUT_ROOT/features_line_nostack_test_rescan.csv" "$v"

  python3 "$SCRIPT" --unit line --M 10 \
    --name "$bench" --csv "$OUT_ROOT/features_unfiltered_M10_test_rescan.csv" "$v"
done

echo "[info] OUT_ROOT=$OUT_ROOT"
echo "[info] SCRIPT=$SCRIPT"

# clean placeholders
find "$OUT_ROOT/traces" -type f -name 'drmem.*.view.log' -size 0 -delete

# start fresh CSVs for this pass
rm -f "$OUT_ROOT/features_line_nostack_test_rescan.csv" \
      "$OUT_ROOT/features_unfiltered_M10_test_rescan.csv"

# iterate over stable, valid gzip files
while IFS= read -r -d '' v; do
  # skip files still incomplete/corrupted
  if ! gzip -t "$v" >/dev/null 2>&1; then
    echo "[skip-bad] $v"
    continue
  fi
  # skip files with no memref lines
  if ! zgrep -qE ' (read|write) +[0-9]+ byte\(s\) @ 0x' "$v" 2>/dev/null; then
    echo "[skip-empty] $v"
    continue
  fi

  bench=$(basename "$v" | cut -d. -f2)

  echo "[feat] $bench <- $(basename "$v")"
  # M=6, exclude stack
  python3 "$SCRIPT" --unit line --exclude-stack --M 6 \
    --name "$bench" --csv "$OUT_ROOT/features_line_nostack_test_rescan.csv" "${v%.gz}" \
    || echo "[warn] M6 failed for $v"

  # M=10, unfiltered
  python3 "$SCRIPT" --unit line --M 10 \
    --name "$bench" --csv "$OUT_ROOT/features_unfiltered_M10_test_rescan.csv" "${v%.gz}" \
    || echo "[warn] M10 failed for $v"

done < <(find "$OUT_ROOT/traces" -maxdepth 1 -type f -name 'drmem.*.view.log.gz' -print0 | sort -z)

echo "[done] features pass"
