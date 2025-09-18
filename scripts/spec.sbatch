#!/usr/bin/env bash
#SBATCH --job-name=miniMXE
#SBATCH --partition=cpu-q
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=72:00:00
#SBATCH --array=0-3
#SBATCH --output=%x-%A_%a.out
#SBATCH --error=%x-%A_%a.err

set -euo pipefail

# Optional: enable your toolchain
# module purge
# module load gcc/13.1.0
# source "$HOME/miniconda3/etc/profile.d/conda.sh" && conda activate base

cd "$SLURM_SUBMIT_DIR"  # repo root

# -----------------------------
# Experiment knobs (env-driven)
# You can override any of these at submit time, e.g.:
#   sbatch --export=ALL,ROI=100,WARMUP_M=30,JANS_L3_SIZE=$((4096*1024)),JANS_L3_LAT=10 scripts/submit_benches.sbatch
# -----------------------------
ROI="${ROI:-30}"                 # measured detailed ROI (in M instructions)
WARMUP_M="${WARMUP_M:-0}"        # warmup (in M instructions) before ROI (0 = no warmup)
SRAM_L3_LAT="${SRAM_L3_LAT:-6}"  # SRAM LLC hit cycles
L3_TAGS_CYC="${L3_TAGS_CYC:-2}"  # tags_access_time (cycles) when setting JanS timing
JANS_L3_SIZE="${JANS_L3_SIZE:-$((16384*1024))}"  # bytes (default 16MB)
JANS_L3_ASSOC="${JANS_L3_ASSOC:-16}"
JANS_L3_LAT="${JANS_L3_LAT:-8}"  # JanS LLC hit cycles and data_access_time
ENABLE_LLC_ENERGY="${ENABLE_LLC_ENERGY:-0}"     # 1 to pass llc e_* keys too

# (Optional) energy constants if ENABLE_LLC_ENERGY=1
SRAM_E_READ="${SRAM_E_READ:-565}"
SRAM_E_WRITE="${SRAM_E_WRITE:-537}"
SRAM_E_MISS="${SRAM_E_MISS:-11}"
SRAM_P_LEAK="${SRAM_P_LEAK:-3438}"
JANS_E_READ="${JANS_E_READ:-188}"
JANS_E_WRITE="${JANS_E_WRITE:-2305}"
JANS_E_MISS="${JANS_E_MISS:-77}"
JANS_P_LEAK="${JANS_P_LEAK:-48}"

# SRAM Size 
SRAM_L3_SIZE="${SRAM_L3_SIZE:-$((8192*1024))}"
export SRAM_L3_SIZE

# Bench array
BENCHES=( "541.leela_r" "531.deepsjeng_r" "520.omnetpp_r" "648.exchange2_s" )
#BENCHES=( "648.exchange2_s" )
#BENCHES=( "541.leela_r" "531.deepsjeng_r" "648.exchange2_s" )
if [[ ${SLURM_ARRAY_TASK_ID:-0} -ge ${#BENCHES[@]} ]]; then
  echo "[ERR] SLURM_ARRAY_TASK_ID=${SLURM_ARRAY_TASK_ID:-unset} out of range"
  exit 1
fi
BENCH="${BENCHES[$SLURM_ARRAY_TASK_ID]}"

# Ensure pipeline is executable
chmod +x scripts/run_spec_pipeline.sh

echo ">>> Running ${BENCH} with ROI=${ROI}M, WARMUP_M=${WARMUP_M}M on $(hostname)"
echo ">>> JanS: size=$((JANS_L3_SIZE/1024))KB assoc=${JANS_L3_ASSOC} lat=${JANS_L3_LAT} cyc, tags=${L3_TAGS_CYC}"
echo ">>> SRAM: lat=${SRAM_L3_LAT} cyc  |  LLC energy flags: ${ENABLE_LLC_ENERGY}"

# Export knobs so the pipeline script picks them up (it reads env or flags)
export WARMUP_M SRAM_L3_LAT L3_TAGS_CYC
export JANS_L3_SIZE JANS_L3_ASSOC JANS_L3_LAT
export ENABLE_LLC_ENERGY SRAM_E_READ SRAM_E_WRITE SRAM_E_MISS SRAM_P_LEAK
export JANS_E_READ JANS_E_WRITE JANS_E_MISS JANS_P_LEAK

# Run (Makefile calls the pipeline and passes core paths; the script consumes the env knobs above)
make run BENCH="$BENCH" N_M="$ROI" BUILD_IF_NEEDED=1 \
     SRAM_L3_SIZE="$SRAM_L3_SIZE" JANS_L3_SIZE="$JANS_L3_SIZE"

echo ">>> Done ${BENCH}"

