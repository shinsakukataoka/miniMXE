#!/usr/bin/env bash
# Trace-only SPEC runner: DynamoRIO memtrace + feature printout (no Sniper)
set -euo pipefail

# -------- Defaults (env-overridable) --------
BENCH=""
SPEC_ROOT="${SPEC_ROOT:-$HOME/spec2017}"
DR_HOME="${DR_HOME:-$HOME/opt/DynamoRIO-Linux-11.3.0-1}"
GCC_DIR="${GCC_DIR:-/cm/local/apps/gcc/13.1.0}"
CONDA_SQLITE_LIB="${CONDA_SQLITE_LIB:-$HOME/miniconda3/lib}"
TMPDIR="${TMPDIR:-$HOME/tmp}"
OUT_ROOT="${OUT_ROOT:-$PWD/results}"
FEATURES_CSV="${FEATURES_CSV:-$PWD/features.csv}"
TRACE_SEC="${TRACE_SEC:-4}"
FEATURES_M="${FEATURES_M:-10}"
BUILD_IF_NEEDED=0
COMPRESS_TRACE="${COMPRESS_TRACE:-1}"   # set to 0 to skip gzip

# -------- Args --------
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

# -------- Pretty logging --------
info(){  echo -e "$*"; }
ok(){    echo -e "\e[32m[OK]\e[0m $*"; }
warn(){  echo -e "\e[33m[WARN]\e[0m $*"; }
err(){   echo -e "\e[31m[ERR]\e[0m $*"; }

export DYNAMORIO_HOME="$DR_HOME"
export LD_LIBRARY_PATH="$CONDA_SQLITE_LIB:${LD_LIBRARY_PATH:-}"

mkdir -p "$OUT_ROOT" "$OUT_ROOT/traces" "$TMPDIR"

SHORT="${BENCH//./_}"
TIMINGS_CSV="$OUT_ROOT/timings.csv"

# --- tiny helper to time any command and append to results/timings.csv ---
timelog_run(){  # timelog_run <label> <csv> -- <cmd...>
  local label="$1"; shift
  local of="$1"; shift
  [[ "${1:-}" == "--" ]] && shift || true
  local start end dur rc ts
  start=$(date +%s.%N)
  "$@" >/dev/null 2>&1
  rc=$?
  end=$(date +%s.%N)
  dur=$(awk -v s="$start" -v e="$end" 'BEGIN{printf "%.3f", (e-s)}')
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  { [[ -f "$of" ]] || echo "timestamp,bench,label,seconds,rc"; echo "$ts,$BENCH,$label,$dur,$rc"; } >> "$of"
  return $rc
}

# -------- Sanity --------
info "\n==== Sanity checks ===="
"$DR_HOME/bin64/drrun" -version >/dev/null && ok "DynamoRIO OK" || { err "DynamoRIO not found at $DR_HOME"; exit 1; }
command -v timeout >/dev/null && ok "coreutils timeout OK" || { err "'timeout' not found"; exit 1; }

if [[ -f "$SPEC_ROOT/shrc" ]]; then
  pushd "$SPEC_ROOT" >/dev/null
  . ./shrc
  popd >/dev/null
else
  err "SPEC shrc not found at $SPEC_ROOT/shrc"
  exit 1
fi
command -v runcpu >/dev/null && ok "SPEC runcpu OK" || { err "SPEC runcpu missing"; exit 1; }

# -------- Locate/build run dir --------
info "\n==== Locate (or build) $BENCH ===="
BENCH_DIR="$SPEC_ROOT/benchspec/CPU/$BENCH"
RUN_ROOT="$BENCH_DIR/run"
if [[ ! -d "$RUN_ROOT" ]] || ! ls -dt "$RUN_ROOT"/run_* >/dev/null 2>&1; then
  if [[ $BUILD_IF_NEEDED -eq 1 ]]; then
    pushd "$SPEC_ROOT" >/dev/null
    runcpu --config my-gcc.cfg --define gcc_dir="$GCC_DIR" --tune base --size test --action build "$BENCH"
    runcpu --config my-gcc.cfg --define gcc_dir="$GCC_DIR" --tune base --size test --action run   "$BENCH"
    popd >/dev/null
  else
    err "No run_* dir and --build-if-needed not set"
    exit 1
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

# Bench-specific fallbacks
if [[ "$BENCH" == "505.mcf_r" && -z "${ARGS:-}" && -f "$RUN_DIR/inp.in" ]]; then
  ARGS="inp.in"
fi
if [[ "$BENCH" == "648.exchange2_s" && -z "${ARGS:-}" ]]; then
  ARGS="2"
fi

ok "Args    : ${ARGS:-<none>}"

# -------- Optional native timing (no tools) --------
info "\n==== Native timing (no tools) ===="
pushd "$RUN_DIR" >/dev/null
timelog_run "native" "$TIMINGS_CSV" -- "$BIN" ${ARGS:+$ARGS} || true
popd >/dev/null

# -------- DynamoRIO trace + features --------
info "\n==== DynamoRIO (${TRACE_SEC}s text trace) + features ===="
pushd "$RUN_DIR" >/dev/null

# Ensure the memtrace client writes logs directly into OUT_ROOT/traces
mkdir -p "$OUT_ROOT/traces"
export DYNAMORIO_OPTIONS="-logdir $OUT_ROOT/traces"

# Marker to capture only new memtrace logs in OUT_ROOT/traces
MARKER="$(mktemp "$TMPDIR/memtrace.marker.XXXX")"; touch "$MARKER"
trap 'rm -f "$MARKER" >/dev/null 2>&1 || true' EXIT

set +e
timelog_run "drrun_memtrace_${TRACE_SEC}s" "$TIMINGS_CSV" -- \
  "$DR_HOME/bin64/drrun" -root "$DR_HOME" -logdir "$OUT_ROOT/traces" -follow_children \
  -c "$DR_HOME/samples/bin64/libmemtrace_x86_text.so" -- \
  /usr/bin/timeout "${TRACE_SEC}s" "$BIN" ${ARGS:+$ARGS}
RC=$?
set -e

# timeout(124) or early finish(0) are both acceptable here
if   [[ $RC -eq 124 ]]; then ok "Trace stopped at timeout (rc=124)"
elif [[ $RC -eq 0   ]]; then ok "Trace completed before timeout"
else err "DynamoRIO run failed (rc=$RC)"; popd >/dev/null; exit $RC; fi

APP="$(basename "$BIN")"

# Find newest memtrace for this app, written after MARKER, in OUT_ROOT/traces
# Find newest memtrace for this app (after MARKER)
mapfile -t TRACE_CANDS < <(
  find "$OUT_ROOT/traces" -maxdepth 1 -type f -newer "$MARKER" -name "memtrace.*${APP}*.log" 2>/dev/null \
  | xargs -r ls -1t 2>/dev/null
)
# Fallback: some DR builds still write to the client dir
if [[ ${#TRACE_CANDS[@]} -eq 0 ]]; then
  mapfile -t TRACE_CANDS < <(
    find "$DR_HOME/samples/bin64" -maxdepth 1 -type f -newer "$MARKER" -name "memtrace.*${APP}*.log" 2>/dev/null \
    | xargs -r ls -1t 2>/dev/null
  )
fi
if [[ ${#TRACE_CANDS[@]} -eq 0 ]]; then
  warn "No new memtrace log found for $APP after run"; popd >/dev/null; exit 0
fi

TRACE_SRC="${TRACE_CANDS[0]}"
DATE_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
TRACE_DST="$OUT_ROOT/traces/${DATE_TAG}_${SHORT}_${TRACE_SEC}s.log"

# Rename to a stable timestamped name (keep original perms/mtime via cp; then remove)
cp -p "$TRACE_SRC" "$TRACE_DST" && rm -f "$TRACE_SRC"
ok "Trace saved: $TRACE_DST"

# Features (optional; depends on mem_metrics_v3.py existing)
if [[ -f "$DR_HOME/samples/mem_metrics_v3.py" ]]; then
  if python3 "$DR_HOME/samples/mem_metrics_v3.py" --name "$BENCH" --csv "$FEATURES_CSV" --M "$FEATURES_M" "$TRACE_DST"; then
    ok "Features appended to: $FEATURES_CSV"
  else
    warn "Feature extraction failed (continuing)"
  fi
else
  warn "mem_metrics_v3.py not found at $DR_HOME/samples/mem_metrics_v3.py; skipping features"
fi

# Optional: compress the (potentially huge) text trace
if [[ "$COMPRESS_TRACE" == "1" ]]; then
  if command -v gzip >/dev/null 2>&1; then
    gzip -f "$TRACE_DST"
    TRACE_DST="${TRACE_DST}.gz"
    ok "Compressed trace: $TRACE_DST"
  else
    warn "gzip not found; leaving trace uncompressed"
  fi
fi

popd >/dev/null

# -------- Tiny printout of what changed --------
echo
echo "---- Trace-only summary ----"
echo "Bench        : $BENCH"
echo "Run dir      : $RUN_DIR"
echo "Binary       : $BIN"
echo "Args         : ${ARGS:-<none>}"
echo "Trace file   : $TRACE_DST"
echo "Timings CSV  : $TIMINGS_CSV"
echo "Features CSV : $FEATURES_CSV"
echo
ok "Done."

