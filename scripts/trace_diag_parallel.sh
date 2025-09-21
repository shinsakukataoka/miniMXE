#!/usr/bin/env bash
# Parallel trace+diagnostics wrapper; writes one consolidated TXT report.
# Usage:
#   ./scripts/trace_diag_parallel.sh 541.leela_r 621.wrf_s ...
#   (or leave args empty to use the default 12 benches below)
#
# Tuning via env:
#   JOBS=4 TRACE_SEC=5 OUT_ROOT="$PWD/results_trace" ./scripts/trace_diag_parallel.sh ...

set -euo pipefail

# ---- knobs (env-overridable) ----
JOBS="${JOBS:-4}"
TRACE_SEC="${TRACE_SEC:-5}"
OUT_ROOT="${OUT_ROOT:-$PWD/results_trace}"
SPEC_ROOT="${SPEC_ROOT:-$HOME/spec2017}"
DR_HOME="${DR_HOME:-$HOME/opt/DynamoRIO-Linux-11.3.0-1}"
RUNNER="${RUNNER:-scripts/run_dr_trace_only.sh}"
FEATURES_CSV="${FEATURES_CSV:-$OUT_ROOT/features_diag_$(date -u +%Y%m%dT%H%M%SZ).csv}"

# benches
if [[ "$#" -gt 0 ]]; then
  BENCHES=("$@")
else
  BENCHES=(
    "541.leela_r" "531.deepsjeng_r" "520.omnetpp_r" "648.exchange2_s"
    "505.mcf_r"   "523.xalancbmk_r" "500.perlbench_r" "502.gcc_r"
    "557.xz_r"    "619.lbm_s"       "621.wrf_s"       "649.fotonik3d_s"
  )
fi

mkdir -p "$OUT_ROOT"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL_TXT="$OUT_ROOT/trace_diag_${ts}.txt"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

# simple semaphore for background jobs
sem() { while (($(jobs -p | wc -l) >= JOBS)); do sleep 0.2; done; }

for bench in "${BENCHES[@]}"; do
  sem
  (
    short="${bench//./_}"
    app_pat="${short#*_}"   # e.g., 541_leela_r -> leela_r
    out="$TMPDIR/${short}.txt"

    {
      echo "==================== $bench ===================="
      echo ">>> run_dr_trace_only.sh (TRACE_SEC=${TRACE_SEC})"
      echo

      # Run the trace with debug enabled; do not abort the bench section on rc!=0
      DR_DEBUG=1 TRACE_SEC="$TRACE_SEC" OUT_ROOT="$OUT_ROOT" FEATURES_CSV="$FEATURES_CSV" \
        "$RUNNER" --bench "$bench" || true
      echo

      # List candidate trace locations
      echo ">>> candidates (possible memtrace locations)"
      run_dir="$(ls -1dt "$SPEC_ROOT/benchspec/CPU/$bench/run"/run_* | head -1 || true)"
      echo "RUN_DIR: ${run_dir:-<none>}"
      if [[ -n "${run_dir:-}" ]]; then
        ls -lt "$run_dir"/memtrace.*"$app_pat"*log 2>/dev/null || echo "(no memtrace in RUN_DIR)"
      fi
      ls -lt "$OUT_ROOT"/traces/memtrace.*"$app_pat"*log 2>/dev/null || echo "(no memtrace in OUT_ROOT/traces)"
      ls -lt "$DR_HOME"/samples/bin64/memtrace.*"$app_pat"*log 2>/dev/null || echo "(no memtrace in DR_HOME/samples/bin64)"
      echo

      # Show first 60 lines of the most recent drrun stderr for this bench/second count
      echo ">>> drrun stderr (first 60 lines)"
      drr_log="$(ls -1t "$OUT_ROOT"/traces/*_"$short"_"${TRACE_SEC}"s.drrun.stderr.log 2>/dev/null | head -1 || true)"
      if [[ -n "${drr_log:-}" ]]; then
        echo "# $drr_log"
        sed -n '1,60p' "$drr_log"
      else
        echo "(no drrun stderr log found for pattern *_${short}_${TRACE_SEC}s.drrun.stderr.log)"
      fi
      echo
    } >"$out" 2>&1
  ) &
done
wait

# Merge per-bench sections in the input order
{
  echo "# trace diagnostics @ ${ts} (TRACE_SEC=${TRACE_SEC}, JOBS=${JOBS})"
  echo "# OUT_ROOT=$OUT_ROOT"
  echo
  for bench in "${BENCHES[@]}"; do
    short="${bench//./_}"
    cat "$TMPDIR/${short}.txt"
  done
} >"$FINAL_TXT"

echo "Wrote: $FINAL_TXT"

