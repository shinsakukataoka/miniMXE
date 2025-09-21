#!/usr/bin/env bash
#SBATCH --job-name=spec-trace
#SBATCH --partition=cpu-q
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=02:00:00
#SBATCH --array=0-11
#SBATCH --output=%x-%A_%a.out
#SBATCH --error=%x-%A_%a.err

set -euo pipefail
cd "$SLURM_SUBMIT_DIR"

BENCHES=(
  "541.leela_r" "531.deepsjeng_r" "520.omnetpp_r" "648.exchange2_s"
  "505.mcf_r"   "523.xalancbmk_r" "500.perlbench_r" "502.gcc_r"
  "557.xz_r"    "619.lbm_s"       "621.wrf_s"       "649.fotonik3d_s"
)

TRACE_SEC="${TRACE_SEC:-10}"
OUT_ROOT="${OUT_ROOT:-$PWD/results_trace}"
FEATURES_M="${FEATURES_M:-10}"
BUILD_IF_NEEDED="${BUILD_IF_NEEDED:-0}"
SPEC_SIZE="${SPEC_SIZE:-test}"

SPEC_ROOT="${SPEC_ROOT:-$HOME/spec2017}"
DR_HOME="${DR_HOME:-$HOME/opt/DynamoRIO-Linux-11.3.0-1}"
GCC_DIR="${GCC_DIR:-/cm/local/apps/gcc/13.1.0}"
CONDA_SQLITE_LIB="${CONDA_SQLITE_LIB:-$HOME/miniconda3/lib}"

RUN_TAG="${RUN_TAG:-$(date -u +%Y%m%dT%H%M%SZ)}"
FEATURES_CSV="${FEATURES_CSV:-$OUT_ROOT/features_${RUN_TAG}.csv}"

if [[ ${SLURM_ARRAY_TASK_ID:-0} -ge ${#BENCHES[@]} ]]; then
  echo "[ERR] SLURM_ARRAY_TASK_ID=${SLURM_ARRAY_TASK_ID:-unset} out of range"; exit 1
fi
BENCH="${BENCHES[$SLURM_ARRAY_TASK_ID]}"

chmod +x scripts/run_dr_trace_only.sh

EXTRA_ARGS=()
[[ "$BUILD_IF_NEEDED" == "1" ]] && EXTRA_ARGS+=(--build-if-needed)

echo ">>> Tracing $BENCH for ${TRACE_SEC}s → CSV=${FEATURES_CSV}"

TRACE_SEC="$TRACE_SEC" OUT_ROOT="$OUT_ROOT" FEATURES_CSV="$FEATURES_CSV" FEATURES_M="$FEATURES_M" \
SPEC_ROOT="$SPEC_ROOT" DR_HOME="$DR_HOME" GCC_DIR="$GCC_DIR" CONDA_SQLITE_LIB="$CONDA_SQLITE_LIB" \
SPEC_SIZE="$SPEC_SIZE" \
  scripts/run_dr_trace_only.sh --bench "$BENCH" "${EXTRA_ARGS[@]}"

