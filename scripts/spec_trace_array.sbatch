#!/usr/bin/env bash
#SBATCH --job-name=spec-drmemtrace-online
#SBATCH --partition=cpu-q
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=02:00:00
#SBATCH --array=0-11
#SBATCH --output=%x-%A_%a.out
#SBATCH --error=%x-%A_%a.err

set -euo pipefail
cd "$SLURM_SUBMIT_DIR"

# Benches
BENCHES=(
  "541.leela_r" "531.deepsjeng_r" "520.omnetpp_r" "648.exchange2_s"
  "505.mcf_r"   "523.xalancbmk_r" "500.perlbench_r" "502.gcc_r"
  "557.xz_r"    "619.lbm_s"       "621.wrf_s"       "649.fotonik3d_s"
)

# === Run settings ===
SPEC_SIZE="${SPEC_SIZE:-test}"      # test|train
CAP_MODE="${CAP_MODE:-instr}"       # instr|time
ROI="${ROI:-500}"                   # ROI M instr (instr mode)
WARMUP_M="${WARMUP_M:-0}"           # warmup M instr (instr mode)
TRACE_SEC="${TRACE_SEC:-120}"       # wall-clock cap (time mode)

OUT_ROOT="${OUT_ROOT:-$PWD/results_trace}"
SPEC_ROOT="${SPEC_ROOT:-$HOME/spec2017}"
DR_HOME="${DR_HOME:-$HOME/opt/DynamoRIO-Linux-11.3.0-1}"
GCC_DIR="${GCC_DIR:-/cm/local/apps/gcc/13.1.0}"

if [[ ${SLURM_ARRAY_TASK_ID:-0} -ge ${#BENCHES[@]} ]]; then
  echo "[ERR] SLURM_ARRAY_TASK_ID out of range"; exit 1
fi
BENCH="${BENCHES[$SLURM_ARRAY_TASK_ID]}"

export SPEC_ROOT DR_HOME GCC_DIR OUT_ROOT SPEC_SIZE CAP_MODE ROI WARMUP_M TRACE_SEC

chmod +x scripts/run_dr_online.sh
echo ">>> Online trace: $BENCH | size=$SPEC_SIZE | mode=$CAP_MODE (${WARMUP_M}M + ${ROI}M OR ${TRACE_SEC}s)"
scripts/run_dr_online.sh --bench "$BENCH"

