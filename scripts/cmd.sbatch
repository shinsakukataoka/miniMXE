#!/usr/bin/env bash
#SBATCH --job-name=miniMXE-cmd
#SBATCH --partition=cpu-q
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=72:00:00
#SBATCH --output=%x-%A_%a.out
#SBATCH --error=%x-%A_%a.err

set -euo pipefail
cd "$SLURM_SUBMIT_DIR"

# ---- Required via --export ----
LABEL="${LABEL:-custom}"                 # label for results files
CMD="${CMD:?set CMD in --export}"        # full command string
CWD="${CWD:-$HOME}"                      # working dir of the app
SIM_N="${SIM_N:-4}"                      # Sniper core count (match threads)

# ---- Common knobs ----
ROI="${ROI:-30}"
WARMUP_M="${WARMUP_M:-0}"
SRAM_L3_LAT="${SRAM_L3_LAT:-6}"
SRAM_L3_SIZE="${SRAM_L3_SIZE:-$((8192*1024))}"
L3_TAGS_CYC="${L3_TAGS_CYC:-2}"
JANS_L3_SIZE="${JANS_L3_SIZE:-$((16384*1024))}"
JANS_L3_ASSOC="${JANS_L3_ASSOC:-16}"
JANS_L3_LAT="${JANS_L3_LAT:-8}"
ENABLE_LLC_ENERGY="${ENABLE_LLC_ENERGY:-0}"

# Energy constants (optional)
SRAM_E_READ="${SRAM_E_READ:-565}";  SRAM_E_WRITE="${SRAM_E_WRITE:-537}"
SRAM_E_MISS="${SRAM_E_MISS:-11}";   SRAM_P_LEAK="${SRAM_P_LEAK:-3438}"
JANS_E_READ="${JANS_E_READ:-188}";  JANS_E_WRITE="${JANS_E_WRITE:-2305}"
JANS_E_MISS="${JANS_E_MISS:-77}";   JANS_P_LEAK="${JANS_P_LEAK:-48}"

# If this looks like a PARSEC tree, set PARSECDIR/PATH (no env.sh!)
if [[ -f "$CWD/bin/parsecmgmt" ]]; then
  export PARSECDIR="$CWD"
  export PATH="$PARSECDIR/bin:$PATH"
  echo ">>> PARSEC: set PARSECDIR=$PARSECDIR and updated PATH"
fi

# Export knobs used by the pipeline
export WARMUP_M SRAM_L3_LAT SRAM_L3_SIZE L3_TAGS_CYC
export JANS_L3_SIZE JANS_L3_ASSOC JANS_L3_LAT
export ENABLE_LLC_ENERGY SRAM_E_READ SRAM_E_WRITE SRAM_E_MISS SRAM_P_LEAK
export JANS_E_READ JANS_E_WRITE JANS_E_MISS JANS_P_LEAK
export SIM_N

echo ">>> Running ${LABEL} | ROI=${ROI}M, WARMUP_M=${WARMUP_M}M, SIM_N=${SIM_N}, CWD=$CWD"
echo ">>> CMD: $CMD"
echo ">>> JanS: size=$((JANS_L3_SIZE/1024))KB assoc=${JANS_L3_ASSOC} lat=${JANS_L3_LAT} cyc, tags=${L3_TAGS_CYC}"
echo ">>> SRAM: size=$((SRAM_L3_SIZE/1024))KB lat=${SRAM_L3_LAT} cyc"

chmod +x scripts/run_spec_pipeline_v2.sh
scripts/run_spec_pipeline_v2.sh \
  --bench "$LABEL" \
  --n-m "$ROI" \
  --cmd "$CMD" \
  --cwd "$CWD" \
  --sim-n "$SIM_N"

echo ">>> Done ${LABEL}"

