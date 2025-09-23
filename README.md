## Quick Start

```bash
git clone [https://github.com/shinsakukataoka/miniMXE.git](https://github.com/shinsakukataoka/miniMXE.git)
cd miniMXE
```

---

## Setup

First, set up **SPEC**, **DynamoRIO**, and **Sniper**.

```bash
# Note your SPEC path.
# All examples use $HOME/spec2017, so change as necessary.
# For example: export SPEC_PATH="$HOME/spec2017"

# 1. Set up DynamoRIO and update your PATH.

# 2. Set up the Sniper fork from:
# [https://github.com/shinsakukataoka/sniper.git](https://github.com/shinsakukataoka/sniper.git)
# ...and then build it.

# 3. Sometimes you need to do the following for a new compile:
ls $HOME/miniconda3/include/sqlite3.h || conda install -y -c conda-forge sqlite
export CPATH="$HOME/miniconda3/include:${CPATH:-}"
export LIBRARY_PATH="$HOME/miniconda3/lib:${LIBRARY_PATH:-}"
export CFLAGS="-I$HOME/miniconda3/include ${CFLAGS:-}"
export CXXFLAGS="-I$HOME/miniconda3/include ${CXXFLAGS:-}"
export LDFLAGS="-L$HOME/miniconda3/lib ${LDFLAGS:-}"
```

---

## Build SPEC Benchmarks

If SPEC is not built yet, run the build script.

```bash
# Usage: ./scripts/spec_build.sh <benchmark_name_1> <benchmark_name_2> ...
# Example:
./scripts/spec_build.sh \
  541.leela_r 531.deepsjeng_r 520.omnetpp_r 648.exchange2_s \
  505.mcf_r 523.xalancbmk_r 500.perlbench_r 502.gcc_r \
  557.xz_r 619.lbm_s 621.wrf_s 649.fotonik3d_s
```

*Note: If the SPEC build fails, try copying `my-gcc.cfg` from the `/patches` directory and overwriting `~/spec2017/config/my-gcc.cfg`.*

---

## Generate DynamoRIO Traces (Optional)

DynamoRIO traces are not necessary for running Sniper but can be useful for exploring correlations.

```bash
# This is a sample sbatch script for generating traces.
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
ln -sf "$DR/samples/bin64/libmemtrace_x86_text.so" "$RAW/libmemtrace_x8GE_text.so"

# Derive args from speccmds.cmd with safe fallbacks
ARGS=""
if [[ -f "$RUN/speccmds.cmd" ]]; then
  LINE=$(grep -m1 -E "../run_base[^ ]+/[^ ]+_base[^ ]+|./[^ ]+_base[^ ]+" "$RUN/speccmds.cmd" || true)
  if [[ -n "$LINE" ]]; then
    LINE_TRIM="${LINE%%>*}"
    ARGS="$(echo "$LINE_TRIM" | sed -E "s@.*_base[^ ]+[[:space:]]*(.*)$@\1@" | xargs || true)"
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

# Rather than above, try this
# This filters stack

# --- env you can tweak ---
export DR="$HOME/opt/DynamoRIO-Linux-11.3.0-1"
export TRACE_SEC=30
export RUN_TAG="$(date -u +%Y%m%dT%H%M%SZ)"

sbatch --job-name=spec-trace-linestack --partition=cpu-q --cpus-per-task=2 --mem=8G --time=02:00:00 \
--array=0-11 \
--output=spec-trace-linestack-%A_%a.out --error=spec-trace-linestack-%A_%a.err \
--export=ALL,DR,TRACE_SEC,RUN_TAG \
--wrap '
set -euo pipefail

OUT="$PWD/results_trace"; mkdir -p "$OUT/traces"
SCRIPT="$SLURM_SUBMIT_DIR/scripts/mem_metrics_unit.py"   # expects your script here

# Output CSVs (separate files per size)
CSV_TEST="$OUT/features_line_nostack_test_${RUN_TAG}.csv"
CSV_TRAIN="$OUT/features_line_nostack_train_${RUN_TAG}.csv"

# Benches (12 entries)
BENCHES=( "541.leela_r" "531.deepsjeng_r" "520.omnetpp_r" "648.exchange2_s"
          "505.mcf_r"   "523.xalancbmk_r" "500.perlbench_r" "502.gcc_r"
          "557.xz_r"    "619.lbm_s"       "621.wrf_s"       "649.fotonik3d_s" )
i=${SLURM_ARRAY_TASK_ID}
BEN=${BENCHES[$i]}

for SIZE in test train; do
  # Resolve a run dir that matches the size; skip if none
  RUN=$(ls -dt "$HOME/spec2017/benchspec/CPU/$BEN/run"/run_*${SIZE}* 2>/dev/null | head -1 || true)
  if [[ -z "${RUN:-}" ]]; then
    echo "[skip] $BEN ($SIZE): no run_*${SIZE}* dir found"
    continue
  fi

  # Binary & args (from this RUN dir)
  BIN=$(find "$RUN" -maxdepth 2 -type f -name "*_base.*" | head -1)
  [[ -z "${BIN:-}" ]] && { echo "[skip] $BEN ($SIZE): no *_base.* binary"; continue; }
  APP=$(basename "$BIN")

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

  # Raw trace dir per bench & size
  RAW="$OUT/traces/diag.raw.${BEN//./_}.${SLURM_JOB_ID}_${SLURM_ARRAY_TASK_ID}.${SIZE}"
  mkdir -p "$RAW"
  ln -sf "$DR/samples/bin64/libmemtrace_x86_text.so" "$RAW/libmemtrace_x86_text.so"

  echo "[run ] $BEN ($SIZE)  RUN=$RUN"
  cd "$RUN"

  # Run: timeout OUTSIDE drrun; logs go under RAW
  /usr/bin/timeout "${TRACE_SEC}s" \
    "$DR/bin64/drrun" -root "$DR" -follow_children \
    -c "$RAW/libmemtrace_x86_text.so" -- \
    "$BIN" ${ARGS:+$ARGS} \
    1>"$RAW/job.stdout" 2>"$RAW/job.stderr" || true

  # Compress thread logs
  gzip -f "$RAW"/memtrace.*.log 2>/dev/null || true

  # Gather all per-thread logs (prefer app-matched), compute features
  mapfile -t LOGS < <(ls -1 "$RAW"/memtrace.*"$APP"*.log.gz 2>/dev/null || true)
  if [[ ${#LOGS[@]} -eq 0 ]]; then
    mapfile -t LOGS < <(ls -1 "$RAW"/memtrace.*.log.gz 2>/dev/null || true)
  fi

  if [[ ${#LOGS[@]} -gt 0 && -f "$SCRIPT" ]]; then
    TMP="$RAW/features.tmp.csv"
    python3 "$SCRIPT" --unit line --exclude-stack --M 6 \
      --name "$BEN" --csv "$TMP" "${LOGS[@]}" || true

    # Append atomically to the size-specific CSV
    OUTCSV="$CSV_TEST"
    [[ "$SIZE" == "train" ]] && OUTCSV="$CSV_TRAIN"

    if [[ -f "$TMP" ]]; then
      exec 9>"$OUTCSV.lock"; flock 9
      if [[ ! -f "$OUTCSV" ]]; then
        cp "$TMP" "$OUTCSV"
      else
        tail -n +2 "$TMP" >> "$OUTCSV"
      fi
      flock -u 9; exec 9>&-
      rm -f "$TMP"
      echo "[feat] appended $BEN ($SIZE) -> $OUTCSV"
    else
      echo "[warn] no features produced for $BEN ($SIZE)"
    fi
  else
    echo "[warn] no logs found for $BEN ($SIZE) in $RAW"
  fi

done

echo "Done: CSV(test)=$CSV_TEST  CSV(train)=$CSV_TRAIN"
'

```

---

## Run Sniper Simulations

Below are a few examples for running test simulations with Sniper.

### Example 1: Iso-Area

This example supposes MRAM is 2x denser than SRAM.
```bash
sbatch --export=ALL,SKIP_TRACE=1,OUT_ROOT="$PWD/results/test_all_spec_benchmarks$(date -u +%Y%m%dT%H%M%SZ)",ROI=100,WARMUP_M=0,SRAM_L3_SIZE=$((8*1024*1024)),SRAM_L3_LAT_RD=4,SRAM_L3_LAT_WR=2,JANS_L3_SIZE=$((4*1024*1024)),JANS_L3_LAT_RD=6,JANS_L3_LAT_WR=17 scripts/run_all.sbatch
```

### Example 2: Iso-Capacity @ 8MB

```bash
sbatch --export=ALL,SKIP_TRACE=1,OUT_ROOT="$PWD/results/test_all_spec_benchmarks_iso8m_$(date -u +%Y%m%dT%H%M%SZ)",ROI=100,WARMUP_M=0,\
SRAM_L3_SIZE=$((8*1024*1024)),SRAM_L3_LAT_RD=4,SRAM_L3_LAT_WR=2,\
JANS_L3_SIZE=$((8*1024*1024)),JANS_L3_LAT_RD=6,JANS_L3_LAT_WR=17 \
scripts/run_all.sbatch
```

### Example 3: Four Example Devices

```bash
# ChungS
sbatch --export=ALL,SKIP_TRACE=1,ENABLE_LLC_ENERGY=1,\
OUT_ROOT="$PWD/results/test_all_spec_benchmarks_iso8m_chungS_custom_energy_$(date -u +%Y%m%dT%H%M%SZ)",ROI=100,WARMUP_M=0,\
SRAM_L3_SIZE=$((8*1024*1024)),SRAM_L3_LAT_RD=4,SRAM_L3_LAT_WR=2,\
JANS_L3_SIZE=$((8*1024*1024)),JANS_L3_LAT_RD=12,JANS_L3_LAT_WR=29,\
JANS_E_READ=397,JANS_E_MISS=127,JANS_E_WRITE=960,JANS_P_LEAK=677 \
scripts/run_all.sbatch

# UmekiS
sbatch --export=ALL,SKIP_TRACE=1,ENABLE_LLC_ENERGY=1,\
OUT_ROOT="$PWD/results/test_all_spec_benchmarks_iso8m_umekiS_custom_energy_$(date -u +%Y%m%dT%H%M%SZ)",ROI=100,WARMUP_M=0,\
SRAM_L3_SIZE=$((8*1024*1024)),SRAM_L3_LAT_RD=4,SRAM_L3_LAT_WR=2,\
JANS_L3_SIZE=$((8*1024*1024)),JANS_L3_LAT_RD=13,JANS_L3_LAT_WR=30,\
JANS_E_READ=397,JANS_E_MISS=90,JANS_E_WRITE=1412,JANS_P_LEAK=1132 \
scripts/run_all.sbatch

# XueS
sbatch --export=ALL,SKIP_TRACE=1,ENABLE_LLC_ENERGY=1,\
OUT_ROOT="$PWD/results/test_all_spec_benchmarks_iso8m_xueS_custom_energy_$(date -u +%Y%m%dT%H%M%SZ)",ROI=100,WARMUP_M=0,\
SRAM_L3_SIZE=$((8*1024*1024)),SRAM_L3_LAT_RD=4,SRAM_L3_LAT_WR=2,\
JANS_L3_SIZE=$((8*1024*1024)),JANS_L3_LAT_RD=15,JANS_L3_LAT_WR=10,\
JANS_E_READ=630,JANS_E_MISS=189,JANS_E_WRITE=677,JANS_P_LEAK=1137 \
scripts/run_all.sbatch

# JanS
sbatch --export=ALL,SKIP_TRACE=1,ENABLE_LLC_ENERGY=1,\
OUT_ROOT="$PWD/results/test_all_spec_benchmarks_iso8m_janS_custom_energy_$(date -u +%Y%m%dT%H%M%SZ)",ROI=100,WARMUP_M=0,\
SRAM_L3_SIZE=$((8*1024*1024)),SRAM_L3_LAT_RD=4,SRAM_L3_LAT_WR=2,\
JANS_L3_SIZE=$((8*1024*1024)),JANS_L3_LAT_RD=14,JANS_L3_LAT_WR=16,\
JANS_E_READ=486,JANS_E_MISS=130,JANS_E_WRITE=1309,JANS_P_LEAK=1129 \
scripts/run_all.sbatch
```

---

## Post-Run Analysis

```bash
# This creates output_* directories.
# summary.csv and energy_bounds.csv will be generated there.
./scripts/run_energy.sh <run_id>
```
Then, open the notebooks.

