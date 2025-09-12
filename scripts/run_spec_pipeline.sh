#!/usr/bin/env bash

set -euo pipefail

# -------- Parse args --------
BENCH=""; N_M=""
SPEC_ROOT="${SPEC_ROOT:-$HOME/spec2017}"
DR_HOME="${DR_HOME:-$HOME/opt/DynamoRIO-Linux-11.3.0-1}"
SNIPER_HOME="${SNIPER_HOME:-$HOME/src/sniper}"
GCC_DIR="${GCC_DIR:-/cm/local/apps/gcc/13.1.0}"
CONDA_SQLITE_LIB="${CONDA_SQLITE_LIB:-$HOME/miniconda3/lib}"
TMPDIR="${TMPDIR:-$HOME/tmp}"
OUT_ROOT="${OUT_ROOT:-$PWD/results}"
FEATURES_CSV="${FEATURES_CSV:-$PWD/features.csv}"
TRACE_SEC="${TRACE_SEC:-4}"
FEATURES_M="${FEATURES_M:-10}"
BUILD_IF_NEEDED=0
JANS_L3_SIZE="${JANS_L3_SIZE:-2097152}"
JANS_L3_ASSOC="${JANS_L3_ASSOC:-16}"
JANS_L3_LAT="${JANS_L3_LAT:-8}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bench) BENCH="$2"; shift 2;;
    --n-m) N_M="$2"; shift 2;;
    --spec-root) SPEC_ROOT="$2"; shift 2;;
    --dr-home) DR_HOME="$2"; shift 2;;
    --sniper-home) SNIPER_HOME="$2"; shift 2;;
    --gcc-dir) GCC_DIR="$2"; shift 2;;
    --conda-sqlite-lib) CONDA_SQLITE_LIB="$2"; shift 2;;
    --tmpdir) TMPDIR="$2"; shift 2;;
    --out-root) OUT_ROOT="$2"; shift 2;;
    --features-csv) FEATURES_CSV="$2"; shift 2;;
    --trace-sec) TRACE_SEC="$2"; shift 2;;
    --features-M) FEATURES_M="$2"; shift 2;;
    --jans-l3-size) JANS_L3_SIZE="$2"; shift 2;;
    --jans-l3-assoc) JANS_L3_ASSOC="$2"; shift 2;;
    --jans-l3-lat) JANS_L3_LAT="$2"; shift 2;;
    --build-if-needed) BUILD_IF_NEEDED=1; shift;;
    *) echo "[ERR] Unknown arg: $1"; exit 1;;
  esac
done

[[ -z "$BENCH" || -z "$N_M" ]] && { echo "Usage: $0 --bench <BENCH> --n-m <N_MILLION> [opts...]"; exit 1; }

info(){ echo -e "$*"; }
ok(){   echo -e "\e[32m[OK]\e[0m $*"; }
warn(){ echo -e "\e[33m[WARN]\e[0m $*"; }
err(){  echo -e "\e[31m[ERR]\e[0m $*"; }

export DYNAMORIO_HOME="$DR_HOME"
export LD_LIBRARY_PATH="$CONDA_SQLITE_LIB:${LD_LIBRARY_PATH:-}"
mkdir -p "$OUT_ROOT" "$TMPDIR" traces

SHORT="${BENCH//./_}"
OUT_SRAM="$OUT_ROOT/${SHORT}_sram_${N_M}M"
OUT_JANS="$OUT_ROOT/${SHORT}_JanS_cap_approx_${N_M}M"
STOP_ICOUNT="$(( N_M * 1000000 ))"

# -------- Sanity --------
info "\n==== Sanity checks ===="
"$DR_HOME/bin64/drrun" -version >/dev/null && ok "DynamoRIO OK" || { err "DynamoRIO not found"; exit 1; }
if [[ -f "$SPEC_ROOT/shrc" ]]; then
  pushd "$SPEC_ROOT" >/dev/null
  . ./shrc
  popd >/dev/null
else
  err "SPEC shrc not found at $SPEC_ROOT/shrc"
  exit 1
fi
command -v runcpu >/dev/null && ok "SPEC runcpu OK" || { err "SPEC runcpu missing"; exit 1; }
if ldd "$SNIPER_HOME/lib/sniper" 2>/dev/null | grep -qi sqlite; then
  ok "sqlite visible to Sniper"
else
  warn "sqlite NOT found by ldd on sniper; using LD_LIBRARY_PATH=$CONDA_SQLITE_LIB"
fi

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
ok "Args    : ${ARGS:-<none>}"

# -------- DynamoRIO trace + features --------
info "\n==== DynamoRIO (${TRACE_SEC}s text trace) + features ===="
pushd "$RUN_DIR" >/dev/null
set +e
"$DR_HOME/bin64/drrun" -root "$DR_HOME" -follow_children -c "$DR_HOME/samples/bin64/libmemtrace_x86_text.so" -- \
  /usr/bin/timeout "${TRACE_SEC}s" "$BIN" ${ARGS:+$ARGS} > /dev/null 2>&1
RC=$?
set -e
if   [[ $RC -eq 124 ]]; then ok "Trace stopped at timeout (rc=124)"
elif [[ $RC -eq 0   ]]; then ok "Trace completed before timeout"
else err "DynamoRIO run failed (rc=$RC)"; exit $RC; fi

APP="$(basename "$BIN")"
TRACEFILE="$(ls -t "$DR_HOME"/samples/bin64/memtrace.*"$APP"*.log 2>/dev/null | head -1 || true)"
if [[ -z "${TRACEFILE:-}" || ! -f "$TRACEFILE" ]]; then
  warn "No memtrace log found for $APP in $DR_HOME/samples/bin64"
else
  ok "Using trace: $TRACEFILE"
  mkdir -p "$OUT_ROOT/traces"
  cp -p "$TRACEFILE" "$OUT_ROOT/traces/"
  python3 "$DR_HOME/samples/mem_metrics_v3.py" --name "$BENCH" --csv "$FEATURES_CSV" --M "$FEATURES_M" "$TRACEFILE" \
    || warn "Feature extraction failed (continuing)"
fi
popd >/dev/null

# -------- Sniper runs --------
run_sniper() {
  local outdir="$1"; shift
  mkdir -p "$outdir"
  pushd "$RUN_DIR" >/dev/null
  "$SNIPER_HOME/run-sniper" -c gainestown -n 1 -d "$outdir" -s "stop-by-icount:${STOP_ICOUNT}" -- "$BIN" ${ARGS:+$ARGS}
  popd >/dev/null
}

info "\n==== Sniper: SRAM baseline (${N_M}M instr) ===="
run_sniper "$OUT_SRAM"

info "\n==== Sniper: JanS L3 cap approx (${N_M}M instr) ===="
mkdir -p "$OUT_JANS"; pushd "$RUN_DIR" >/dev/null
"$SNIPER_HOME/run-sniper" -c gainestown -n 1 \
  -c perf_model/l3_cache/size="$JANS_L3_SIZE" \
  -c perf_model/l3_cache/associativity="$JANS_L3_ASSOC" \
  -c perf_model/l3_cache/latency="$JANS_L3_LAT" \
  -d "$OUT_JANS" -s "stop-by-icount:${STOP_ICOUNT}" -- "$BIN" ${ARGS:+$ARGS}
popd >/dev/null

# -------- Summaries --------
info "\n==== Results summary (Sniper) ===="
summary_header() {
  printf "%-10s\t%-10s\t%-10s\t%-6s\t%-12s\t%-10s\t%-10s\t%-6s\t%-10s\t%-12s\n" \
    "Config" "Instr" "Cycles" "IPC" "Time(ns)" "L3_acc" "L3_miss" "L3MR%" "DRAM_acc" "DRAM_lat(ns)"
}

summarize() {
  local tag="$1"; local dir="$2"; local f="$2/sim.out"
  if [[ ! -f "$f" ]]; then
    printf "%-10s\t%-10s\t%-10s\t%-6s\t%-12s\t%-10s\t%-10s\t%-6s\t%-10s\t%-12s\n" \
      "$tag" "MISSING" "-" "-" "-" "-" "-" "-" "-" "-"; return
  fi
  local instr cycles ipc tns l3acc l3mis l3mr dramacc dramlat
  instr=$(grep -m1 -E "^[[:space:]]*Instructions[[:space:]]*\|" "$f" | awk -F'|' '{gsub(/[ \t]/,"",$2); print $2}')
  cycles=$(grep -m1 -E "^[[:space:]]*Cycles[[:space:]]*\|" "$f" | awk -F'|' '{gsub(/[ \t]/,"",$2); print $2}')
  ipc=$(grep -m1 -E "^[[:space:]]*IPC[[:space:]]*\|" "$f" | awk -F'|' '{gsub(/[ \t]/,"",$2); print $2}')
  tns=$(grep -m1 -E "^[[:space:]]*Time \(ns\)[[:space:]]*\|" "$f" | awk -F'|' '{gsub(/[ \t]/,"",$2); print $2}')
  l3acc=$(awk '/Cache L3/{f=1;next} f && /num cache accesses/ {print $NF; f=0}' "$f")
  l3mis=$(awk '/Cache L3/{f=1;next} f && /num cache misses/   {print $NF; f=0}' "$f")
  l3mr=$( awk '/Cache L3/{f=1;next} f && /miss rate/          {print $(NF); f=0}' "$f" | tr -d '%')
  dramacc=$(awk '/DRAM summary/{f=1;next} f && /num dram accesses/ {print $NF; f=0}' "$f")
  dramlat=$(awk '/DRAM summary/{f=1;next} f && /average dram access latency/ {print $(NF-1)}' "$f")

  printf "%-10s\t%-10s\t%-10s\t%-6s\t%-12s\t%-10s\t%-10s\t%-6s\t%-10s\t%-12s\n" \
    "$tag" "${instr:-NA}" "${cycles:-NA}" "${ipc:-NA}" "${tns:-NA}" \
    "${l3acc:-NA}" "${l3mis:-NA}" "${l3mr:-NA}" "${dramacc:-NA}" "${dramlat:-NA}"

  # Append to CSV (results/summary.csv)
  local csv="$OUT_ROOT/summary.csv"; local ts
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  {
    if [[ ! -f "$csv" ]]; then echo "timestamp,bench,n_m,config,instructions,cycles,ipc,time_ns,l3_acc,l3_miss,l3_miss_rate_pct,dram_acc,dram_lat_ns,outdir"; fi
    echo "$ts,$BENCH,$N_M,$tag,${instr:-},${cycles:-},${ipc:-},${tns:-},${l3acc:-},${l3mis:-},${l3mr:-},${dramacc:-},${dramlat:-},$dir"
  } >> "$csv"
}

summary_header
summarize "SRAM" "$OUT_SRAM"
summarize "JanS" "$OUT_JANS"

# -------- Energy / ED^2P (bounds) --------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 "$REPO_ROOT/scripts/energy_ed2p.py" "$OUT_SRAM/sim.out" "$OUT_JANS/sim.out" || echo "[WARN] Energy/ED^2P calculation failed"

ok "Done."

