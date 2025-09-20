#!/usr/bin/env bash
#SBATCH --job-name=miniMXE-unified
#SBATCH --partition=cpu-q
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=72:00:00
#SBATCH --array=0-22
#SBATCH --output=%x-%A_%a.out
#SBATCH --error=%x-%A_%a.err

set -euo pipefail
cd "$SLURM_SUBMIT_DIR"  # repo root

# -------------------------------------------
# Mode selection:
#   If CMD is provided via --export, we run a single generic command (CMD mode)
#   Otherwise we run SPEC benches via SLURM array (SPEC mode)
# -------------------------------------------
CMD="${CMD:-}"           # full command string (set to enable CMD mode)
LABEL="${LABEL:-custom}"       # label for CMD mode results
CWD="${CWD:-$HOME}"            # working dir for CMD mode
SIM_N="${SIM_N:-4}"            # Sniper cores for CMD mode

# -------- Common knobs (both modes) --------
ROI="${ROI:-100}"              # measured ROI (M instructions)
WARMUP_M="${WARMUP_M:-0}"      # warmup (M instructions)

SRAM_L3_LAT="${SRAM_L3_LAT:-6}"
SRAM_L3_SIZE="${SRAM_L3_SIZE:-$((8192*1024))}"

# Asymmetric LLC read/write (defaults keep symmetric)
SRAM_L3_LAT_RD="${SRAM_L3_LAT_RD:-$SRAM_L3_LAT}"
SRAM_L3_LAT_WR="${SRAM_L3_LAT_WR:-$SRAM_L3_LAT}"

L3_TAGS_CYC="${L3_TAGS_CYC:-2}"
MRAM_L3_SIZE="${MRAM_L3_SIZE:-$((16384*1024))}"
MRAM_L3_ASSOC="${MRAM_L3_ASSOC:-16}"
MRAM_L3_LAT="${MRAM_L3_LAT:-8}"

# Asymmetric for MRAM too
MRAM_L3_LAT_RD="${MRAM_L3_LAT_RD:-$MRAM_L3_LAT}"
MRAM_L3_LAT_WR="${MRAM_L3_LAT_WR:-$MRAM_L3_LAT}"

ENABLE_LLC_ENERGY="${ENABLE_LLC_ENERGY:-0}"

# Energy constants (optional)
SRAM_E_READ="${SRAM_E_READ:-565}";  SRAM_E_WRITE="${SRAM_E_WRITE:-537}"
SRAM_E_MISS="${SRAM_E_MISS:-11}";   SRAM_P_LEAK="${SRAM_P_LEAK:-3438}"
MRAM_E_READ="${MRAM_E_READ:-188}";  MRAM_E_WRITE="${MRAM_E_WRITE:-2305}"
MRAM_E_MISS="${MRAM_E_MISS:-77}";   MRAM_P_LEAK="${MRAM_P_LEAK:-48}"

# SPEC-only helpers (ignored in CMD mode)
SPEC_SIZE="${SPEC_SIZE:-ref}"       # test | train | ref
BUILD_IF_NEEDED="${BUILD_IF_NEEDED:-1}"

# Export knobs used by the pipeline
export WARMUP_M SRAM_L3_LAT SRAM_L3_SIZE L3_TAGS_CYC
export MRAM_L3_SIZE MRAM_L3_ASSOC MRAM_L3_LAT
export ENABLE_LLC_ENERGY SRAM_E_READ SRAM_E_WRITE SRAM_E_MISS SRAM_P_LEAK
export MRAM_E_READ MRAM_E_WRITE MRAM_E_MISS MRAM_P_LEAK
export SIM_N SPEC_SIZE
export SRAM_L3_LAT_RD SRAM_L3_LAT_WR MRAM_L3_LAT_RD MRAM_L3_LAT_WR

chmod +x scripts/run_spec_pipeline.sh

if [[ -n "$CMD" ]]; then
  # ======================== CMD MODE ========================
  echo ">>> [CMD MODE] LABEL=$LABEL | ROI=${ROI}M, WARMUP_M=${WARMUP_M}M, SIM_N=${SIM_N}, CWD=$CWD"
  echo ">>> CMD: $CMD"
  echo ">>> MRAM: size=$((MRAM_L3_SIZE/1024))KB assoc=${MRAM_L3_ASSOC} read=${MRAM_L3_LAT_RD}c write=${MRAM_L3_LAT_WR}c, tags=${L3_TAGS_CYC}"
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
  # This list now contains all benchmarks from the paper's Table V
  BENCHES=(
    "602.bzip2_s" "605.mcf_s" "620.omnetpp_s" "623.xalancbmk_s"
    "625.x264_s" "631.deepsjeng_s" "641.leela_s" "648.exchange2_s"
    "657.xz_s" "999.specrand_s" "603.bwaves_s" "607.cactuBSSN_s"
    "619.lbm_s" "621.wrf_s" "627.cam4_s" "628.pop2_s" "638.imagick_s"
    "644.nab_s" "649.fotonik3d_s" "600.perlbench_s" "605.mcf_s"
    "631.deepsjeng_s" "641.leela_s"
  )

  # *** MODIFIED LINE ***
  # Use the BENCH variable if it's passed in, otherwise use the array
  BENCH="${BENCH:-${BENCHES[$SLURM_ARRAY_TASK_ID]}}"

  if [[ ${SLURM_ARRAY_TASK_ID:-0} -ge ${#BENCHES[@]} && -z "${BENCH:-}" ]]; then
    echo "[WARN] SLURM_ARRAY_TASK_ID=${SLURM_ARRAY_TASK_ID:-unset} out of range, but continuing since BENCH may be set."
  fi

  echo ">>> [SPEC MODE] Running ${BENCH} | ROI=${ROI}M, WARMUP_M=${WARMUP_M}M, SPEC_SIZE=${SPEC_SIZE} on $(hostname)"
  echo ">>> MRAM: size=$((MRAM_L3_SIZE/1024))KB assoc=${MRAM_L3_ASSOC} read=${MRAM_L3_LAT_RD}c write=${MRAM_L3_LAT_WR}c, tags=${L3_TAGS_CYC}"
  echo ">>> SRAM: size=$((SRAM_L3_SIZE/1024))KB read=${SRAM_L3_LAT_RD}c write=${SRAM_L3_LAT_WR}c  |  LLC energy flags: ${ENABLE_LLC_ENERGY}"

  scripts/run_spec_pipeline.sh \
    --bench "$BENCH" \
    --n-m "$ROI" \
    --build-if-needed \
    --spec-size "$SPEC_SIZE"

  echo ">>> Done ${BENCH}"
fi

