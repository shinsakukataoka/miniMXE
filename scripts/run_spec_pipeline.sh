#!/usr/bin/env bash
set -euo pipefail

# -------- Defaults --------
BENCH=""; N_M=""; CMD=""; CWD=""
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
SIM_N="${SIM_N:-1}"
SPEC_SIZE="${SPEC_SIZE:-test}"   # test | train | ref
SKIP_TRACE="${SKIP_TRACE:-0}"    # 1 = skip DynamoRIO memtrace stage

# SRAM baseline knobs (size-aware)
SRAM_L3_SIZE="${SRAM_L3_SIZE:-$((8192*1024))}"   # bytes; convert to KB
SRAM_L3_LAT="${SRAM_L3_LAT:-6}"

# --- Asymmetric LLC hit cycles (defaults keep symmetric behavior) ---
SRAM_L3_LAT_RD="${SRAM_L3_LAT_RD:-$SRAM_L3_LAT}"
SRAM_L3_LAT_WR="${SRAM_L3_LAT_WR:-$SRAM_L3_LAT}"

# JanS knobs
JANS_L3_SIZE="${JANS_L3_SIZE:-16777216}"         # bytes; convert to KB (16 MB default)
JANS_L3_ASSOC="${JANS_L3_ASSOC:-16}"
JANS_L3_LAT="${JANS_L3_LAT:-8}"                  # cycles
L3_TAGS_CYC="${L3_TAGS_CYC:-2}"                  # tags_access_time (cycles)

# --- Asymmetric for JanS as well (defaults to symmetric) ---
JANS_L3_LAT_RD="${JANS_L3_LAT_RD:-$JANS_L3_LAT}"
JANS_L3_LAT_WR="${JANS_L3_LAT_WR:-$JANS_L3_LAT}"

# ROI/warmup
WARMUP_M="${WARMUP_M:-0}"                         # million instr warmup before ROI (0 = none)

# Optional: LLC energy model flags
ENABLE_LLC_ENERGY="${ENABLE_LLC_ENERGY:-0}"
SRAM_E_READ="${SRAM_E_READ:-565}"
SRAM_E_WRITE="${SRAM_E_WRITE:-537}"
SRAM_E_MISS="${SRAM_E_MISS:-11}"
SRAM_P_LEAK="${SRAM_P_LEAK:-3438}"
JANS_E_READ="${JANS_E_READ:-188}"
JANS_E_WRITE="${JANS_E_WRITE:-2305}"
JANS_E_MISS="${JANS_E_MISS:-77}"
JANS_P_LEAK="${JANS_P_LEAK:-48}"

# -------- Arg parse --------
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
    --jans-l3-lat-rd) JANS_L3_LAT_RD="$2"; shift 2;;
    --jans-l3-lat-wr) JANS_L3_LAT_WR="$2"; shift 2;;
    --sram-l3-size) SRAM_L3_SIZE="$2"; shift 2;;
    --sram-l3-lat) SRAM_L3_LAT="$2"; shift 2;;
    --sram-l3-lat-rd) SRAM_L3_LAT_RD="$2"; shift 2;;
    --sram-l3-lat-wr) SRAM_L3_LAT_WR="$2"; shift 2;;
    --l3-tags-cyc) L3_TAGS_CYC="$2"; shift 2;;
    --warmup-m) WARMUP_M="$2"; shift 2;;
    --enable-llc-energy) ENABLE_LLC_ENERGY=1; shift;;
    --build-if-needed) BUILD_IF_NEEDED=1; shift;;
    --cmd) CMD="$2"; shift 2;;
    --cwd) CWD="$2"; shift 2;;
    --sim-n) SIM_N="$2"; shift 2;;
    --spec-size) SPEC_SIZE="$2"; shift 2;;
    --skip-trace) SKIP_TRACE=1; shift;;
    *) echo "[ERR] Unknown arg: $1"; exit 1;;
  esac
done

[[ -z "$BENCH" || -z "$N_M" ]] && { echo "Usage: $0 --bench <LABEL|SPEC_BENCH> --n-m <N_MILLION> [--cmd <full command>] [--cwd <dir>] [--sim-n <cores>] [--build-if-needed] [--skip-trace] [opts...]"; exit 1; }

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
TIMINGS_CSV="$OUT_ROOT/timings.csv"

# Stop spec string
if [[ "${WARMUP_M}" =~ ^[0-9]+$ ]] && [[ "$WARMUP_M" -gt 0 ]]; then
  STOP_SPEC="stop-by-icount:${N_M}M:${WARMUP_M}M"
else
  STOP_SPEC="stop-by-icount:${STOP_ICOUNT}"
fi

# --- tiny helper to time any command and append to results/timings.csv ---
timelog_run(){  # timelog_run <label> <csv> -- <cmd...>
  local label="$1"; shift
  local of="$1"; shift
  [[ "$1" == "--" ]] && shift || true
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
if [[ -z "$CMD" ]]; then
  if [[ -f "$SPEC_ROOT/shrc" ]]; then
    pushd "$SPEC_ROOT" >/dev/null; . ./shrc; popd >/dev/null
  else
    err "SPEC shrc not found at $SPEC_ROOT/shrc"; exit 1
  fi
  command -v runcpu >/dev/null && ok "SPEC runcpu OK" || { err "SPEC runcpu missing"; exit 1; }
fi
if [[ "$SKIP_TRACE" != "1" ]]; then
  "$DR_HOME/bin64/drrun" -version >/dev/null && ok "DynamoRIO OK" || { err "DynamoRIO not found"; exit 1; }
else
  ok "Skipping DynamoRIO checks (--skip-trace)"
fi

if ldd "$SNIPER_HOME/lib/sniper" 2>/dev/null | grep -qi sqlite; then
  ok "sqlite visible to Sniper"
else
  warn "sqlite NOT found by ldd on sniper; using LD_LIBRARY_PATH=$CONDA_SQLITE_LIB"
fi

# -------- Locate workload --------
info "\n==== Locate workload ===="
if [[ -z "$CMD" ]]; then
  # SPEC path
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
else
  # Generic command path
  RUN_DIR="${CWD:-$PWD}"
  ok "CWD: $RUN_DIR"
fi

# -------- Resolve command (SPEC vs generic) --------
ARGS=""; APP=""; declare -a APP_CMD
if [[ -z "$CMD" ]]; then
  # SPEC
  mapfile -t BIN_CANDIDATES < <(find "$RUN_DIR" -maxdepth 2 -type f -name '*_base.*' | sort)
  [[ ${#BIN_CANDIDATES[@]} -gt 0 ]] || { err "No *_base.* binary in $RUN_DIR"; exit 1; }
  BIN="${BIN_CANDIDATES[0]}"; ok "Binary : $BIN"

  if [[ -f "$RUN_DIR/speccmds.cmd" ]]; then
    LINE=$(grep -m1 -E '../run_base[^ ]+/[^ ]+_base[^ ]+|./[^ ]+_base[^ ]+' "$RUN_DIR/speccmds.cmd" || true)
    if [[ -n "${LINE:-}" ]]; then
      LINE_TRIM="${LINE%%>*}"
      ARGS="$(echo "$LINE_TRIM" | sed -E 's@.*_base[^ ]+[[:space:]]*(.*)$@\1@' | xargs || true)"
    fi
  fi
  [[ -z "${ARGS:-}" && -f "$RUN_DIR/test.txt" ]] && ARGS="test.txt"
  [[ -z "${ARGS:-}" && -f "$RUN_DIR/test.sgf" ]] && ARGS="test.sgf"
  if [[ "$BENCH" == "648.exchange2_s" && -z "${ARGS:-}" ]]; then ARGS="2"; fi
  ok "Args    : ${ARGS:-<none>}"
  APP="$(basename "$BIN")"
  APP_CMD=("$BIN" ${ARGS:+$ARGS})
else
  ok "Command : $CMD"
  CMD_EXPANDED="$(bash -lc 'printf %s "$CMD"')"
  read -r -a TOK <<< "$CMD_EXPANDED"
  if [[ ${#TOK[@]} -gt 0 ]] && command -v "${TOK[0]}" >/dev/null 2>&1; then
    APP_CMD=("${TOK[@]}")
    APP="$(basename "${TOK[0]}")"
    ok "Resolved exec: ${APP_CMD[*]}"
  else
    APP_CMD=(/bin/bash -lc "$CMD")
    FIRST="$(awk '{print $1}' <<< "$CMD")"
    APP="$(basename "$FIRST")"
    warn "Falling back to shell launch via /bin/bash -lc"
  fi
fi

# -------- Native timing (no tools) --------
info "\n==== Native timing (no tools) ===="
pushd "$RUN_DIR" >/dev/null
timelog_run "native" "$TIMINGS_CSV" -- "${APP_CMD[@]}" || true
popd >/dev/null

# -------- DynamoRIO trace + features --------
if [[ "$SKIP_TRACE" != "1" ]]; then
  info "\n==== DynamoRIO (${TRACE_SEC}s text trace) + features ===="
  pushd "$RUN_DIR" >/dev/null
  set +e
  timelog_run "drrun_memtrace_${TRACE_SEC}s" "$TIMINGS_CSV" -- \
    "$DR_HOME/bin64/drrun" -root "$DR_HOME" -follow_children -c "$DR_HOME/samples/bin64/libmemtrace_x86_text.so" -- \
    /usr/bin/timeout "${TRACE_SEC}s" "${APP_CMD[@]}"
  RC=$?
  set -e
  if   [[ $RC -eq 124 ]]; then ok "Trace stopped at timeout (rc=124)"
  elif [[ $RC -eq 0   ]]; then ok "Trace completed before timeout"
  else err "DynamoRIO run failed (rc=$RC)"; popd >/dev/null; exit $RC; fi

  # Find trace file robustly: look in DR samples dir and CWD; prefer one matching APP, else newest
  TRACEFILE="$(ls -t "$DR_HOME"/samples/bin64/memtrace.*"$APP"*.log "$RUN_DIR"/memtrace.*"$APP"*.log 2>/dev/null | head -1 || true)"
  if [[ -z "${TRACEFILE:-}" ]]; then
    TRACEFILE="$(ls -t "$DR_HOME"/samples/bin64/memtrace.*.log "$RUN_DIR"/memtrace.*.log 2>/dev/null | head -1 || true)"
  fi
  if [[ -z "${TRACEFILE:-}" || ! -f "$TRACEFILE" ]]; then
    warn "No memtrace log found for $APP in either $DR_HOME/samples/bin64 or $RUN_DIR"
  else
    ok "Using trace: $TRACEFILE"
    mkdir -p "$OUT_ROOT/traces"
    cp -p "$TRACEFILE" "$OUT_ROOT/traces/"
    python3 "$DR_HOME/samples/mem_metrics_v3.py" --name "$BENCH" --csv "$FEATURES_CSV" --M "$FEATURES_M" "$TRACEFILE" \
      || warn "Feature extraction failed (continuing)"
  fi
  popd >/dev/null
else
  info "\n==== Skipping DynamoRIO trace (--skip-trace) ===="
fi

# -------- Sniper runs --------
run_sniper() {
  local outdir="$1"; shift
  mkdir -p "$outdir"
  pushd "$RUN_DIR" >/dev/null
  "$SNIPER_HOME/run-sniper" -c gainestown -n "$SIM_N" \
    -d "$outdir" \
    "$@" \
    -s "$STOP_SPEC" \
    -- "${APP_CMD[@]}" \
    >"$outdir/sniper.log" 2>&1
  local rc=$?
  popd >/dev/null
  return $rc
}

build_energy_flags() {
  local which="$1"  # "sram" or "jans"
  [[ "$ENABLE_LLC_ENERGY" != "1" ]] && return 0
  if [[ "$which" == "sram" ]]; then
    echo -g perf_model/l3_cache/llc/e_read_hit_pJ="$SRAM_E_READ" \
         -g perf_model/l3_cache/llc/e_write_hit_pJ="$SRAM_E_WRITE" \
         -g perf_model/l3_cache/llc/e_miss_pJ="$SRAM_E_MISS" \
         -g perf_model/l3_cache/llc/p_leak_mW="$SRAM_P_LEAK"
  else
    echo -g perf_model/l3_cache/llc/e_read_hit_pJ="$JANS_E_READ" \
         -g perf_model/l3_cache/llc/e_write_hit_pJ="$JANS_E_WRITE" \
         -g perf_model/l3_cache/llc/e_miss_pJ="$JANS_E_MISS" \
         -g perf_model/l3_cache/llc/p_leak_mW="$JANS_P_LEAK"
  fi
}

info "\n==== Sniper: SRAM baseline (${N_M}M instr${WARMUP_M:+, warmup ${WARMUP_M}M}, n=${SIM_N}) ===="
set +e
timelog_run "sniper_sram" "$TIMINGS_CSV" -- \
  run_sniper "$OUT_SRAM" \
    -g perf_model/l3_cache/cache_size=$(( SRAM_L3_SIZE / 1024 )) \
    -g perf_model/l3_cache/llc/read_hit_latency_cycles="$SRAM_L3_LAT_RD" \
    -g perf_model/l3_cache/llc/write_hit_latency_cycles="$SRAM_L3_LAT_WR" \
    $(build_energy_flags sram)
RC_SRAM=$?
set -e
[[ $RC_SRAM -ne 0 ]] && err "Sniper SRAM failed (rc=$RC_SRAM). See $OUT_SRAM/sniper.log"

info "\n==== Sniper: JanS L3 cap approx (${N_M}M instr${WARMUP_M:+, warmup ${WARMUP_M}M}, n=${SIM_N}) ===="
set +e
timelog_run "sniper_jans" "$TIMINGS_CSV" -- \
  run_sniper "$OUT_JANS" \
    -g perf_model/l3_cache/cache_size=$(( JANS_L3_SIZE / 1024 )) \
    -g perf_model/l3_cache/associativity="${JANS_L3_ASSOC}" \
    -g perf_model/l3_cache/tags_access_time="${L3_TAGS_CYC}" \
    -g perf_model/l3_cache/data_access_time="${JANS_L3_LAT}" \
    -g perf_model/l3_cache/llc/read_hit_latency_cycles="${JANS_L3_LAT_RD}" \
    -g perf_model/l3_cache/llc/write_hit_latency_cycles="${JANS_L3_LAT_WR}" \
    $(build_energy_flags jans)
RC_JANS=$?
set -e
[[ $RC_JANS -ne 0 ]] && err "Sniper JanS failed (rc=$RC_JANS). See $OUT_JANS/sniper.log"

# -------- Results summary --------
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

  l3acc=$(awk -F'\\|' '
    /Cache L3/ {f=1; next}
    f && /num cache accesses/ {
      sum=0; for(i=2;i<=NF;i++){g=$i; gsub(/[^0-9]/,"",g); if(length(g)) sum+=g}
      print sum; exit
    }' "$f")

  l3mis=$(awk -F'\\|' '
    /Cache L3/ {f=1; next}
    f && /num cache misses/ {
      sum=0; for(i=2;i<=NF;i++){g=$i; gsub(/[^0-9]/,"",g); if(length(g)) sum+=g}
      print sum; exit
    }' "$f")

  if [[ -n "${l3acc:-}" && -n "${l3mis:-}" && "$l3acc" -gt 0 ]]; then
    l3mr=$(awk -v a="$l3acc" -v m="$l3mis" 'BEGIN{printf "%.2f", 100*m/a}')
  else
    l3mr="NA"
  fi

  dramacc=$(awk -F'\\|' '
    /DRAM summary/ {f=1; next}
    f && /num dram accesses/ {
      sum=0; for(i=2;i<=NF;i++){g=$i; gsub(/[^0-9]/,"",g); if(length(g)) sum+=g}
      print sum; exit
    }' "$f")

  dramlat=$(awk -F'\\|' '
    /DRAM summary/ {f=1; next}
    f && /average dram access latency/ {
      g=$2; gsub(/[[:space:]]/,"",g); sub(/[^0-9.].*/,"",g); print g; exit
    }' "$f")

  printf "%-10s\t%-10s\t%-10s\t%-6s\t%-12s\t%-10s\t%-10s\t%-6s\t%-10s\t%-12s\n" \
    "$tag" "${instr:-NA}" "${cycles:-NA}" "${ipc:-NA}" "${tns:-NA}" \
    "${l3acc:-NA}" "${l3mis:-NA}" "${l3mr:-NA}" "${dramacc:-NA}" "${dramlat:-NA}"

  local csv="$OUT_ROOT/summary.csv"; local ts
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  {
    if [[ ! -f "$csv" ]]; then
      echo "timestamp,bench,n_m,config,instructions,cycles,ipc,time_ns,l3_acc,l3_miss,l3_miss_rate_pct,dram_acc,dram_lat_ns,outdir,warmup_m,sram_l3_lat,jans_l3_lat,jans_l3_kb,jans_assoc,sram_l3_lat_rd,sram_l3_lat_wr,jans_l3_lat_rd,jans_l3_lat_wr"
    fi
    echo "$ts,$BENCH,$N_M,$tag,${instr:-},${cycles:-},${ipc:-},${tns:-},${l3acc:-},${l3mis:-},${l3mr:-},${dramacc:-},${dramlat:-},$dir,$WARMUP_M,$SRAM_L3_LAT,$JANS_L3_LAT,$(( JANS_L3_SIZE/1024 )),$JANS_L3_ASSOC,$SRAM_L3_LAT_RD,$SRAM_L3_LAT_WR,$JANS_L3_LAT_RD,$JANS_L3_LAT_WR"
  } >> "$csv"
}

summary_header
summarize "SRAM" "$OUT_SRAM"
summarize "JanS" "$OUT_JANS"

echo
ok "Done."

