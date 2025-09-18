#!/usr/bin/env bash
#SBATCH --job-name=spec-trace
#SBATCH --partition=cpu-q
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=02:00:00
#SBATCH --array=0-3
#SBATCH --output=%x-%A_%a.out
#SBATCH --error=%x-%A_%a.err

cd "$SLURM_SUBMIT_DIR"
BENCHES=( "631.deepsjeng_s" "541.leela_r" "648.exchange2_s" "505.mcf_r" )
BENCH="${BENCHES[$SLURM_ARRAY_TASK_ID]}"
TRACE_SEC="${TRACE_SEC:-5}"
OUT_ROOT="${OUT_ROOT:-$PWD/results_trace}"
FEATURES_CSV="${FEATURES_CSV:-$PWD/features_trace.csv}"

chmod +x scripts/dr_trace_only.sh
TRACE_SEC="$TRACE_SEC" OUT_ROOT="$OUT_ROOT" FEATURES_CSV="$FEATURES_CSV" \
  scripts/run_dr_trace_only.sh --bench "$BENCH"

