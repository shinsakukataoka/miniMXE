Quick start
```
git clone https://github.com/shinsakukataoka/miniMXE.git
cd miniMXE
```
If SPEC is not built yet, do this:
```
./scripts/spec_build.sh <benchmark_name>
# example
./scripts/spec_build.sh \ 541.leela_r 531.deepsjeng_r 520.omnetpp_r 648.exchange2_s \ 505.mcf_r 523.xalancbmk_r 500.perlbench_r 502.gcc_r \ 557.xz_r 619.lbm_s 621.wrf_s 649.fotonik3d_s
```
DynamoRIO trace is not necessary for running Sniper, but useful for exploring correlation:
```
# I know this is extremely dirty, but do this
export DR="$HOME/opt/DynamoRIO-Linux-11.3.0-1"
export TRACE_SEC=5
export RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
sbatch --job-name=spec-trace-fixed --partition=cpu-q --cpus-per-task=2 --mem=8G --time=02:00:00 \
--array=0-11 \
--output=spec-trace-fixed-%A_%a.out --error=spec-trace-fixed-%A_%a.err \
--export=ALL,DR,TRACE_SEC,RUN_TAG \
--wrap '
set -euo pipefail
OUT="$PWD/results_trace"; mkdir -p "$OUT/traces"
FEATURES="$OUT/features_${RUN_TAG}.csv"

# Bench list (12 entries)
BENCHES=( "541.leela_r" "531.deepsjeng_r" "520.omnetpp_r" "648.exchange2_s"
          "505.mcf_r"   "523.xalancbmk_r" "500.perlbench_r" "502.gcc_r"
          "557.xz_r"    "619.lbm_s"       "621.wrf_s"       "649.fotonik3d_s" )
i=${SLURM_ARRAY_TASK_ID}
BEN=${BENCHES[$i]}

# Resolve run dir + binary
RUN=$(ls -dt "$HOME/spec2017/benchspec/CPU/$BEN/run"/run_* | head -1)
BIN=$(find "$RUN" -maxdepth 2 -type f -name "*_base.*" | head -1)
APP=$(basename "$BIN")
RAW="$OUT/traces/diag.raw.$(echo "$BEN"|tr . _).${SLURM_JOB_ID}_${SLURM_ARRAY_TASK_ID}"
mkdir -p "$RAW"
ln -sf "$DR/samples/bin64/libmemtrace_x86_text.so" "$RAW/libmemtrace_x86_text.so"

# Derive args from speccmds.cmd with safe fallbacks
ARGS=""
if [[ -f "$RUN/speccmds.cmd" ]]; then
  LINE=$(grep -m1 -E "../run_base[^ ]+/[^ ]+_base[^ ]+|./[^ ]+_base[^ ]+" "$RUN/speccmds.cmd" || true)
  if [[ -n "$LINE" ]]; then
    LINE_TRIM="${LINE%%>*}"
    ARGS="$(echo "$LINE_TRIM" | sed -E "s@.*_base[^ ]+[[:space:]]*(.*)$@\\1@" | xargs || true)"
  fi
fi
[[ -z "$ARGS" && -f "$RUN/test.txt" ]] && ARGS="test.txt"
[[ -z "$ARGS" && -f "$RUN/test.sgf" ]] && ARGS="test.sgf"
case "$BEN" in
  648.exchange2_s) [[ -z "$ARGS" ]] && ARGS="2" ;;
  505.mcf_r)       [[ -z "$ARGS" && -f "$RUN/inp.in" ]] && ARGS="inp.in" ;;
  557.xz_r)
    if [[ -z "$ARGS" ]]; then
      XZ=$(ls -1 "$RUN"/*.xz 2>/dev/null | head -1 || true)
      [[ -n "$XZ" ]] && ARGS="-dkc $(basename "$XZ")"
    fi
    ;;
  619.lbm_s)
    if [[ -z "$ARGS" ]]; then
      IN=$(ls -1 "$RUN"/*.in 2>/dev/null | head -1 || true)
      [[ -n "$IN" ]] && ARGS="$(basename "$IN")"
    fi
    ;;
esac

# Launch: timeout OUTSIDE drrun; client from RAW so logs land in RAW
cd "$RUN"
/usr/bin/timeout "${TRACE_SEC}s" \
  "$DR/bin64/drrun" -root "$DR" -follow_children \
  -c "$RAW/libmemtrace_x86_text.so" -- \
  "$BIN" ${ARGS:+$ARGS} \
  1>"$RAW/job.stdout" 2>"$RAW/job.stderr" || true

# Pick the app log (not the timeout one)
F=$(ls -t "$RAW"/memtrace.*"$APP"*.log 2>/dev/null | head -1 || true)

# Append features row (create header if missing)
if [[ -n "$F" && -f "$DR/samples/mem_metrics_v3.py" ]]; then
  TMP="$RAW/features.tmp.csv"
  python3 "$DR/samples/mem_metrics_v3.py" --name "$BEN" --csv "$TMP" --M 10 "$F" || true
  if [[ -f "$TMP" ]]; then
    # serialize append
    exec 9>"$FEATURES.lock"; flock 9
    if [[ ! -f "$FEATURES" ]]; then
      cp "$TMP" "$FEATURES"
    else
      tail -n +2 "$TMP" >> "$FEATURES"
    fi
    flock -u 9; exec 9>&-
    rm -f "$TMP"
  fi
fi

# Compress raw logs to save space
gzip -f "$RAW"/memtrace.*.log 2>/dev/null || true

echo ">>> done $BEN | RAW=$RAW"
'

```
Then, you can run some test sniper runs
```
# iso area, supposing MRAM is 2x dense than SRAM
sbatch --export=ALL,SKIP_TRACE=1,OUT_ROOT="$PWD/results/test_all_spec_benchmarks$(date -u +%Y%m%dT%H%M%SZ)",ROI=100,WARMUP_M=0,SRAM_L3_SIZE=$((8*1024*1024)),SRAM_L3_LAT_RD=4,SRAM_L3_LAT_WR=2,JANS_L3_SIZE=$((4*1024*1024)),JANS_L3_LAT_RD=6,JANS_L3_LAT_WR=17 scripts/run_all.sbatch
# iso cap @ 8MB
sbatch --export=ALL,SKIP_TRACE=1,OUT_ROOT="$PWD/results/test_all_spec_benchmarks_iso8m_$(date -u +%Y%m%dT%H%M%SZ)",ROI=100,WARMUP_M=0,\
SRAM_L3_SIZE=$((8*1024*1024)),SRAM_L3_LAT_RD=4,SRAM_L3_LAT_WR=2,\
JANS_L3_SIZE=$((8*1024*1024)),JANS_L3_LAT_RD=6,JANS_L3_LAT_WR=17 \
scripts/run_all.sbatch
# run for four example devices
sbatch --export=ALL,SKIP_TRACE=1,ENABLE_LLC_ENERGY=1,\
OUT_ROOT="$PWD/results/test_all_spec_benchmarks_iso8m_chungS_custom_energy_$(date -u +%Y%m%dT%H%M%SZ)",ROI=100,WARMUP_M=0,\
SRAM_L3_SIZE=$((8*1024*1024)),SRAM_L3_LAT_RD=4,SRAM_L3_LAT_WR=2,\
JANS_L3_SIZE=$((8*1024*1024)),JANS_L3_LAT_RD=12,JANS_L3_LAT_WR=29,\
JANS_E_READ=397,JANS_E_MISS=127,JANS_E_WRITE=960,JANS_P_LEAK=677 \
scripts/run_all.sbatch

sbatch --export=ALL,SKIP_TRACE=1,ENABLE_LLC_ENERGY=1,\
OUT_ROOT="$PWD/results/test_all_spec_benchmarks_iso8m_umekiS_custom_energy_$(date -u +%Y%m%dT%H%M%SZ)",ROI=100,WARMUP_M=0,\
SRAM_L3_SIZE=$((8*1024*1024)),SRAM_L3_LAT_RD=4,SRAM_L3_LAT_WR=2,\
JANS_L3_SIZE=$((8*1024*1024)),JANS_L3_LAT_RD=13,JANS_L3_LAT_WR=30,\
JANS_E_READ=397,JANS_E_MISS=90,JANS_E_WRITE=1412,JANS_P_LEAK=1132 \
scripts/run_all.sbatch

sbatch --export=ALL,SKIP_TRACE=1,ENABLE_LLC_ENERGY=1,\
OUT_ROOT="$PWD/results/test_all_spec_benchmarks_iso8m_xueS_custom_energy_$(date -u +%Y%m%dT%H%M%SZ)",ROI=100,WARMUP_M=0,\
SRAM_L3_SIZE=$((8*1024*1024)),SRAM_L3_LAT_RD=4,SRAM_L3_LAT_WR=2,\
JANS_L3_SIZE=$((8*1024*1024)),JANS_L3_LAT_RD=15,JANS_L3_LAT_WR=10,\
JANS_E_READ=630,JANS_E_MISS=189,JANS_E_WRITE=677,JANS_P_LEAK=1137 \
scripts/run_all.sbatch

sbatch --export=ALL,SKIP_TRACE=1,ENABLE_LLC_ENERGY=1,\
OUT_ROOT="$PWD/results/test_all_spec_benchmarks_iso8m_janS_custom_energy_$(date -u +%Y%m%dT%H%M%SZ)",ROI=100,WARMUP_M=0,\
SRAM_L3_SIZE=$((8*1024*1024)),SRAM_L3_LAT_RD=4,SRAM_L3_LAT_WR=2,\
JANS_L3_SIZE=$((8*1024*1024)),JANS_L3_LAT_RD=14,JANS_L3_LAT_WR=16,\
JANS_E_READ=486,JANS_E_MISS=130,JANS_E_WRITE=1309,JANS_P_LEAK=1129 \
scripts/run_all.sbatch
```
After the run:
```
# this creates output_* directories, and summary.csv and energy_bounds.csv will be generated tbere
./scripts/run_energy.sh <run_id>
# open notebooks for exploration

