#!/usr/bin/env bash
#SBATCH --job-name=miniMXE-unified
#SBATCH --partition=cpu-q
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=72:00:00
#SBATCH --array=0-3
#SBATCH --output=%x-%A_%a.out
#SBATCH --error=%x-%A_%a.err

set -euo pipefail
cd "$SLURM_SUBMIT_DIR"  # repo root

# -------------------------------------------
# Mode selection:
#   If CMD is provided via --export, we run a single generic command (CMD mode)
#   Otherwise we run SPEC benches via SLURM array (SPEC mode)
# -------------------------------------------
CMD="${CMD:-}"                 # full command string (set to enable CMD mode)
LABEL="${LABEL:-custom}"       # label for CMD mode results
CWD="${CWD:-$HOME}"            # working dir for CMD mode
SIM_N="${SIM_N:-4}"            # Sniper cores for CMD mode

# -------- Common knobs (both modes) --------
ROI="${ROI:-30}"               # measured ROI (M instructions)
WARMUP_M="${WARMUP_M:-0}"      # warmup (M instructions)

SRAM_L3_LAT="${SRAM_L3_LAT:-6}"
SRAM_L3_SIZE="${SRAM_L3_SIZE:-$((8192*1024))}"

# Asymmetric LLC read/write (defaults keep symmetric)
SRAM_L3_LAT_RD="${SRAM_L3_LAT_RD:-$SRAM_L3_LAT}"
SRAM_L3_LAT_WR="${SRAM_L3_LAT_WR:-$SRAM_L3_LAT}"

L3_TAGS_CYC="${L3_TAGS_CYC:-2}"
JANS_L3_SIZE="${JANS_L3_SIZE:-$((16384*1024))}"
JANS_L3_ASSOC="${JANS_L3_ASSOC:-16}"
JANS_L3_LAT="${JANS_L3_LAT:-8}"

# Asymmetric for JanS too
JANS_L3_LAT_RD="${JANS_L3_LAT_RD:-$JANS_L3_LAT}"
JANS_L3_LAT_WR="${JANS_L3_LAT_WR:-$JANS_L3_LAT}"

ENABLE_LLC_ENERGY="${ENABLE_LLC_ENERGY:-0}"

# Energy constants (optional)
SRAM_E_READ="${SRAM_E_READ:-565}";  SRAM_E_WRITE="${SRAM_E_WRITE:-537}"
SRAM_E_MISS="${SRAM_E_MISS:-11}";   SRAM_P_LEAK="${SRAM_P_LEAK:-3438}"
JANS_E_READ="${JANS_E_READ:-188}";  JANS_E_WRITE="${JANS_E_WRITE:-2305}"
JANS_E_MISS="${JANS_E_MISS:-77}";   JANS_P_LEAK="${JANS_P_LEAK:-48}"

# SPEC-only helpers (ignored in CMD mode)
SPEC_SIZE="${SPEC_SIZE:-test}"      # test | train | ref
BUILD_IF_NEEDED="${BUILD_IF_NEEDED:-1}"

# Export knobs used by the pipeline
export WARMUP_M SRAM_L3_LAT SRAM_L3_SIZE L3_TAGS_CYC
export JANS_L3_SIZE JANS_L3_ASSOC JANS_L3_LAT
export ENABLE_LLC_ENERGY SRAM_E_READ SRAM_E_WRITE SRAM_E_MISS SRAM_P_LEAK
export JANS_E_READ JANS_E_WRITE JANS_E_MISS JANS_P_LEAK
export SIM_N SPEC_SIZE

# NEW: export asymmetric envs
export SRAM_L3_LAT_RD SRAM_L3_LAT_WR JANS_L3_LAT_RD JANS_L3_LAT_WR

chmod +x scripts/run_spec_pipeline.sh

if [[ -n "$CMD" ]]; then
  # ======================== CMD MODE ========================
  echo ">>> [CMD MODE] LABEL=$LABEL | ROI=${ROI}M, WARMUP_M=${WARMUP_M}M, SIM_N=${SIM_N}, CWD=$CWD"
  echo ">>> CMD: $CMD"
  echo ">>> JanS: size=$((JANS_L3_SIZE/1024))KB assoc=${JANS_L3_ASSOC} read=${JANS_L3_LAT_RD}c write=${JANS_L3_LAT_WR}c, tags=${L3_TAGS_CYC}"
  echo ">>> SRAM: size=$((SRAM_L3_SIZE/1024))KB read=${SRAM_L3_LAT_RD}c write=${SRAM_L3_LAT_WR}c"

  scripts/run_spec_pipeline.sh \
    --bench "$LABEL" \
    --n-m "$ROI" \
    --cmd "$CMD" \
    --cwd "$CWD" \
    --sim-n "$SIM_N"

  echo ">>> Done ${LABEL}"
else
  # ======================== SPEC MODE ========================
  BENCHES=( "541.leela_r" "531.deepsjeng_r" "520.omnetpp_r" "648.exchange2_s" )

  if [[ ${SLURM_ARRAY_TASK_ID:-0} -ge ${#BENCHES[@]} ]]; then
    echo "[ERR] SLURM_ARRAY_TASK_ID=${SLURM_ARRAY_TASK_ID:-unset} out of range"
    exit 1
  fi
  BENCH="${BENCHES[$SLURM_ARRAY_TASK_ID]}"

  echo ">>> [SPEC MODE] Running ${BENCH} | ROI=${ROI}M, WARMUP_M=${WARMUP_M}M, SPEC_SIZE=${SPEC_SIZE} on $(hostname)"
  echo ">>> JanS: size=$((JANS_L3_SIZE/1024))KB assoc=${JANS_L3_ASSOC} read=${JANS_L3_LAT_RD}c write=${JANS_L3_LAT_WR}c, tags=${L3_TAGS_CYC}"
  echo ">>> SRAM: size=$((SRAM_L3_SIZE/1024))KB read=${SRAM_L3_LAT_RD}c write=${SRAM_L3_LAT_WR}c  |  LLC energy flags: ${ENABLE_LLC_ENERGY}"

  scripts/run_spec_pipeline.sh \
    --bench "$BENCH" \
    --n-m "$ROI" \
    --build-if-needed \
    --spec-size "$SPEC_SIZE"

  echo ">>> Done ${BENCH}"
fi

