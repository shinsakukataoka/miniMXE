#!/usr/bin/env bash
# Trace-only SPEC runner: DynamoRIO memtrace + features (no Sniper)
set -euo pipefail

# -------- Defaults (env-overridable) --------
BENCH=""
SPEC_ROOT="${SPEC_ROOT:-$HOME/spec2017}"
DR_HOME="${DR_HOME:-$HOME/opt/DynamoRIO-Linux-11.3.0-1}"
GCC_DIR="${GCC_DIR:-/cm/local/apps/gcc/13.1.0}"
CONDA_SQLITE_LIB="${CONDA_SQLITE_LIB:-$HOME/miniconda3/lib}"
TMPDIR="${TMPDIR:-$HOME/tmp}"
OUT_ROOT="${OUT_ROOT:-$PWD/results_trace}"
FEATURES_CSV="${FEATURES_CSV:-$OUT_ROOT/features_${SLURM_JOB_ID:-$$}.csv}"
TRACE_SEC="${TRACE_SEC:-10}"
FEATURES_M="${FEATURES_M:-10}"
BUILD_IF_NEEDED=0
COMPRESS_TRACE="${COMPRESS_TRACE:-1}"
DR_DEBUG="${DR_DEBUG:-0}"
SPEC_SIZE="${SPEC_SIZE:-test}"

# -------- Arg parse --------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bench) BENCH="$2"; shift 2;;
    --spec-root) SPEC_ROOT="$2"; shift 2;;
    --dr-home) DR_HOME="$2"; shift 2;;
    --gcc-dir) GCC_DIR="$2"; shift 2;;
    --conda-sqlite-lib) CONDA_SQLITE_LIB="$2"; shift 2;;
    --tmpdir) TMPDIR="$2"; shift 2;;
    --out-root) OUT_ROOT="$2"; shift 2;;
    --features-csv) FEATURES_CSV="$2"; shift 2;;
    --trace-sec) TRACE_SEC="$2"; shift 2;;
    --features-M) FEATURES_M="$2"; shift 2;;
    --build-if-needed) BUILD_IF_NEEDED=1; shift;;
    *) echo "[ERR] Unknown arg: $1" >&2; exit 1;;
  esac
done
[[ -z "$BENCH" ]] && { echo "Usage: $0 --bench <SPEC_BENCH> [opts...]"; exit 1; }

# -------- Logging helpers --------
info(){  echo -e "$*"; }
ok(){    echo -e "\e[32m[OK]\e[0m $*"; }
warn(){  echo -e "\e[33m[WARN]\e[0m $*"; }
err(){   echo -e "\e[31m[ERR]\e[0m $*"; }

export DYNAMORIO_HOME="$DR_HOME"
export LD_LIBRARY_PATH="$CONDA_SQLITE_LIB:${LD_LIBRARY_PATH:-}"

mkdir -p "$OUT_ROOT" "$OUT_ROOT/traces" "$TMPDIR"

SHORT="${BENCH//./_}"
TIMINGS_CSV="$OUT_ROOT/timings.csv"

# Unique RAW dir on node-local TMP, ensure logs land *here* by loading client from this dir
RAW_ID="${SLURM_ARRAY_TASK_ID:-$PPID}"
RAW_DIR="$TMPDIR/drraw.${RAW_ID}.${SHORT}"
mkdir -p "$RAW_DIR"
ln -sf "$DR_HOME/samples/bin64/libmemtrace_x86_text.so" "$RAW_DIR/libmemtrace_x86_text.so"
info "[INFO] DR memtrace RAW dir (node-local): $RAW_DIR"

# -------- Sanity --------
info "\n==== Sanity checks ===="
"$DR_HOME/bin64/drrun" -version >/dev/null && ok "DynamoRIO OK" || { err "DynamoRIO not found at $DR_HOME"; exit 1; }
command -v timeout >/dev/null && ok "coreutils timeout OK" || { err "'timeout' not found"; exit 1; }

if [[ -f "$SPEC_ROOT/shrc" ]]; then pushd "$SPEC_ROOT" >/dev/null; . ./shrc; popd >/dev/null
else err "SPEC shrc not found at $SPEC_ROOT/shrc"; exit 1; fi
command -v runcpu >/dev/null && ok "SPEC runcpu OK" || { err "SPEC runcpu missing"; exit 1; }

# -------- Locate/build run dir --------
info "\n==== Locate (or build) $BENCH ===="
BENCH_DIR="$SPEC_ROOT/benchspec/CPU/$BENCH"
RUN_ROOT="$BENCH_DIR/run"
if [[ ! -d "$RUN_ROOT" ]] || ! ls -dt "$RUN_ROOT"/run_* >/dev/null 2>&1; then
  if [[ $BUILD_IF_NEEDED -eq 1 ]]; then
    pushd "$SPEC_ROOT" >/dev/null
    runcpu --config my-gcc.cfg --define gcc_dir="$GCC_DIR" --tune base --size "$SPEC_SIZE" --action build "$BENCH"
    runcpu --config my-gcc.cfg --define gcc_dir="$GCC_DIR" --tune base --size "$SPEC_SIZE" --action run   "$BENCH"
    popd >/dev/null
  else
    err "No run_* dir and --build-if-needed not set"; exit 1
  fi
fi

RUN_DIR="$(ls -dt "$RUN_ROOT"/run_* | head -1)"
[[ -d "$RUN_DIR" ]] || { err "Run dir not found"; exit 1; }
ok "Run dir: $RUN_DIR"

# -------- Find binary & args --------
mapfile -t BIN_CANDIDATES < <(find "$RUN_DIR" -maxdepth 2 -type f -name '*_base.*' | sort)
[[ ${#BIN_CANDIDATES[@]} -gt 0 ]] || { err "No *_base.* binary in $RUN_DIR"; exit 1; }
BIN="${BIN_CANDIDATES[0]}"; ok "Binary : $BIN"

ARGS=""
if [[ -f "$RUN_DIR/speccmds.cmd" ]]; then
  LINE=$(grep -m1 -E '../run_base[^ ]+/[^ ]+_base[^ ]+|./[^ ]+_base[^ ]+' "$RUN_DIR/speccmds.cmd" || true)
  if [[ -n "${LINE:-}" ]]; then
    LINE_TRIM="${LINE%%>*}"
    ARGS="$(echo "$LINE_TRIM" | sed -E 's@.*_base[^ ]+[[:space:]]*(.*)$@\1@' | xargs || true)"
  fi
fi
[[ -z "${ARGS:-}" && -f "$RUN_DIR/test.txt" ]] && ARGS="test.txt"
[[ -z "${ARGS:-}" && -f "$RUN_DIR/test.sgf" ]] && ARGS="test.sgf"
if [[ -z "${ARGS:-}" ]]; then
  case "$BENCH" in
    648.exchange2_s) ARGS="2" ;;
    505.mcf_r) [[ -f "$RUN_DIR/inp.in" ]] && ARGS="inp.in" ;;
    557.xz_r)
      xz_in=$(ls -1 "$RUN_DIR"/*.xz 2>/dev/null | head -1 || true)
      [[ -n "$xz_in" ]] && ARGS="-dkc $(basename "$xz_in")"
      ;;
    619.lbm_s)
      lbm_in=$(ls -1 "$RUN_DIR"/*.in 2>/dev/null | head -1 || true)
      [[ -n "$lbm_in" ]] && ARGS="$(basename "$lbm_in")"
      ;;
  esac
fi
ok "Args    : ${ARGS:-<none>}"

# -------- Optional native timing (no tools) --------
info "\n==== Native timing (no tools) ===="
pushd "$RUN_DIR" >/dev/null
start=$(date +%s.%N); ( "$BIN" ${ARGS:+$ARGS} ) >/dev/null 2>&1 || true; end=$(date +%s.%N)
dur=$(awk -v s="$start" -v e="$end" 'BEGIN{printf "%.3f", (e-s)}')
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{ [[ -f "$TIMINGS_CSV" ]] || echo "timestamp,bench,label,seconds,rc"; echo "$ts,$BENCH,native,$dur,0"; } >> "$TIMINGS_CSV"
popd >/dev/null

# -------- DynamoRIO trace + features --------
info "\n==== DynamoRIO (${TRACE_SEC}s text trace) + features ===="
pushd "$RUN_DIR" >/dev/null

DATE_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
DRR_LOG="$OUT_ROOT/traces/${DATE_TAG}_${SHORT}_${TRACE_SEC}s.drrun.stderr.log"

set +e
start=$(date +%s.%N)
"$DR_HOME/bin64/drrun" ${DR_DEBUG:+-debug -verbose 2} \
  -root "$DR_HOME" -follow_children \
  -c "$RAW_DIR/libmemtrace_x86_text.so" -- \
  /usr/bin/timeout "${TRACE_SEC}s" "$BIN" ${ARGS:+$ARGS} \
  1>"$RAW_DIR/runner.stdout.log" 2>"$DRR_LOG"
RC=$?
end=$(date +%s.%N)
set -e

dur=$(awk -v s="$start" -v e="$end" 'BEGIN{printf "%.3f", (e-s)}')
ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{ [[ -f "$TIMINGS_CSV" ]] || echo "timestamp,bench,label,seconds,rc";
  echo "$ts,$BENCH,drrun_memtrace_${TRACE_SEC}s,$dur,${RC:-0}"; } >> "$TIMINGS_CSV"

APP="$(basename "$BIN")"

# Collect all per-thread logs from RAW_DIR (prefer app-matched)
mapfile -t TRACE_SRCS < <(ls -1t "$RAW_DIR"/memtrace.*"$APP"*.log 2>/dev/null || true)
if [[ ${#TRACE_SRCS[@]} -eq 0 ]]; then
  mapfile -t TRACE_SRCS < <(ls -1t "$RAW_DIR"/memtrace.*.log 2>/dev/null || true)
fi
[[ ${#TRACE_SRCS[@]} -eq 0 ]] && { err "No memtrace logs in $RAW_DIR"; exit 2; }

TRACE_DST_TMP="$RAW_DIR/${DATE_TAG}_${SHORT}_${TRACE_SEC}s.allthreads.log"
cat "${TRACE_SRCS[@]}" > "$TRACE_DST_TMP"
ok "Merged trace (all threads): $TRACE_DST_TMP"

# ---- Features: compute then append under a short lock ----
if [[ -f "$DR_HOME/samples/mem_metrics_v3.py" ]]; then
  TMP_FEATURES="$RAW_DIR/features.tmp.${DATE_TAG}_${SHORT}.csv"
  python3 "$DR_HOME/samples/mem_metrics_v3.py" --name "$BENCH" --csv "$TMP_FEATURES" --M "$FEATURES_M" "$TRACE_DST_TMP" || warn "Feature extraction failed"

  if [[ -f "$TMP_FEATURES" ]]; then
    mkdir -p "$(dirname "$FEATURES_CSV")"
    if command -v flock >/dev/null 2>&1; then
      exec 9>"$FEATURES_CSV.lock"
      flock 9
      if [[ ! -f "$FEATURES_CSV" ]]; then
        cp "$TMP_FEATURES" "$FEATURES_CSV"
      else
        tail -n +2 "$TMP_FEATURES" >> "$FEATURES_CSV"
      fi
      flock -u 9
      exec 9>&-
    else
      { [[ -f "$FEATURES_CSV" ]] || head -n1 "$TMP_FEATURES" > "$FEATURES_CSV"; tail -n +2 "$TMP_FEATURES" >> "$FEATURES_CSV"; }
    fi
    rm -f "$TMP_FEATURES"
    ok "Features appended to: $FEATURES_CSV"
  fi
else
  warn "mem_metrics_v3.py not found at $DR_HOME/samples/mem_metrics_v3.py; skipping features"
fi

# Move/Compress final trace artifact to OUT_ROOT
FINAL_DST="$OUT_ROOT/traces/${DATE_TAG}_${SHORT}_${TRACE_SEC}s.allthreads.log"
mkdir -p "$OUT_ROOT/traces"
mv -f "$TRACE_DST_TMP" "$FINAL_DST"
if [[ "$COMPRESS_TRACE" == "1" ]] && command -v gzip >/dev/null 2>&1; then
  gzip -f "$FINAL_DST"; FINAL_DST="${FINAL_DST}.gz"; ok "Compressed trace: $FINAL_DST"
fi

popd >/dev/null

echo
echo "---- Trace-only summary ----"
echo "Bench        : $BENCH"
echo "Run dir      : $RUN_DIR"
echo "Binary       : $BIN"
echo "Args         : ${ARGS:-<none>}"
echo "Trace file   : $FINAL_DST"
echo "Timings CSV  : $TIMINGS_CSV"
echo "Features CSV : $FEATURES_CSV"
echo
ok "Done."

