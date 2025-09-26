#!/usr/bin/env bash
# Online SPEC runner: drmemtrace + rwstats (no offline dumps)
set -euo pipefail

# -------- Defaults (env-overridable) --------
BENCH=""
SPEC_ROOT="${SPEC_ROOT:-$HOME/spec2017}"
DR_HOME="${DR_HOME:-$HOME/opt/DynamoRIO-Linux-11.3.0-1}"
GCC_DIR="${GCC_DIR:-/cm/local/apps/gcc/13.1.0}"
TMPDIR="${TMPDIR:-$HOME/tmp}"
OUT_ROOT="${OUT_ROOT:-$PWD/results_trace}"
SPEC_SIZE="${SPEC_SIZE:-test}"

# Caps: choose CAP_MODE=instr or CAP_MODE=time
CAP_MODE="${CAP_MODE:-instr}"          # instr|time
WARMUP_M="${WARMUP_M:-0}"              # warmup M instr (instr mode)
ROI="${ROI:-500}"                      # ROI M instr    (instr mode)
TRACE_SEC="${TRACE_SEC:-120}"          # wall-clock cap (time mode)

DR_DEBUG="${DR_DEBUG:-0}"

# Analyzer knobs (override via env if you like)
: "${RWSTATS_INTERVAL:=5000000}"             # snapshot every 5M memrefs (0=only final)
: "${RWSTATS_STRIDE_CAP_BYTES:=$((1<<20))}"  # clamp big strides at 1MB
: "${RWSTATS_DISABLE_STRIDE:=0}"             # 1 to disable stride stats (saves memory)

# -------- Logging helpers --------
ok(){   echo -e "\e[32m[OK]\e[0m $*"; }
warn(){ echo -e "\e[33m[WARN]\e[0m $*"; }
err(){  echo -e "\e[31m[ERR]\e[0m $*"; }

# -------- Arg parse --------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bench) BENCH="$2"; shift 2;;
    --spec-root) SPEC_ROOT="$2"; shift 2;;
    --dr-home) DR_HOME="$2"; shift 2;;
    --tmpdir) TMPDIR="$2"; shift 2;;
    --out-root) OUT_ROOT="$2"; shift 2;;
    --cap-mode) CAP_MODE="$2"; shift 2;;
    --warmup-M) WARMUP_M="$2"; shift 2;;
    --roi-M) ROI="$2"; shift 2;;
    --trace-sec) TRACE_SEC="$2"; shift 2;;
    *) err "Unknown arg: $1"; exit 1;;
  esac
done
[[ -z "$BENCH" ]] && { echo "Usage: $0 --bench <SPEC_BENCH> [opts...]"; exit 1; }

# -------- Env --------
export DYNAMORIO_HOME="$DR_HOME"
export LD_LIBRARY_PATH="$DR_HOME/tools/lib64/release:$DR_HOME/lib64/release:${LD_LIBRARY_PATH:-}"
# GCC runtime (cluster-specific)
export LD_LIBRARY_PATH="$GCC_DIR/lib64:${LD_LIBRARY_PATH:-}"
export RWSTATS_INTERVAL RWSTATS_STRIDE_CAP_BYTES RWSTATS_DISABLE_STRIDE

mkdir -p "$OUT_ROOT" "$OUT_ROOT/logs" "$TMPDIR"

SHORT="${BENCH//./_}"
DATE_TAG="$(date -u +%Y%m%dT%H%M%SZ)"
RAW_ID="${SLURM_ARRAY_TASK_ID:-$PPID}"
RAW_DIR="$TMPDIR/drraw.${RAW_ID}.${SHORT}"
mkdir -p "$RAW_DIR"
DRR_LOG="$OUT_ROOT/logs/${DATE_TAG}_${SHORT}_${CAP_MODE}.rwstats.log"

# -------- Sanity --------
"$DR_HOME/bin64/drrun" -version >/dev/null || { err "DynamoRIO not found at $DR_HOME"; exit 1; }
if [[ -f "$SPEC_ROOT/shrc" ]]; then pushd "$SPEC_ROOT" >/dev/null; . ./shrc; popd >/dev/null
else err "SPEC shrc not found at $SPEC_ROOT/shrc"; exit 1; fi
command -v runcpu >/dev/null || { err "SPEC runcpu missing"; exit 1; }
command -v timeout >/dev/null || { warn "'timeout' not found; time caps will be skipped"; true; }

# -------- Locate run dir --------
BENCH_DIR="$SPEC_ROOT/benchspec/CPU/$BENCH"
RUN_ROOT="$BENCH_DIR/run"
[[ -d "$RUN_ROOT" ]] || { err "No run dir for $BENCH"; exit 1; }
RUN_DIR="$(ls -dt "$RUN_ROOT"/run_* | head -1)"
[[ -d "$RUN_DIR" ]] || { err "Run dir not found"; exit 1; }

# -------- Find command from speccmds.cmd (SPEC) --------
# Parse SPEC's specinvoke recipe: use -C as cwd; take the first real app line (-o ... -e ...).
if [[ -f "$RUN_DIR/speccmds.cmd" ]]; then
  RUN_CWD="$(awk '$1=="-C"{dir=$2} END{print dir}' "$RUN_DIR/speccmds.cmd")"
  RUN_CWD="${RUN_CWD:-$RUN_DIR}"

  CMD_LINE="$(awk '
    /^[[:space:]]*-o[[:space:]]+/ {
      line=$0
      sub(/^[ \t]*-o[ \t]+\S+[ \t]+-e[ \t]+\S+[ \t]+/, "", line)  # drop -o/-e
      sub(/[ \t]*>[ \t].*$/, "", line)                           # drop > redir
      sub(/[ \t]*2>>[ \t].*$/, "", line)                         # drop 2>> redir
      print line; exit
    }' "$RUN_DIR/speccmds.cmd")"

  [[ -n "${CMD_LINE:-}" ]] || { err "Failed to parse speccmds.cmd for $BENCH"; exit 1; }

  ok "Run dir : $RUN_DIR"
  ok "Run cwd : $RUN_CWD"
  ok "Command : $CMD_LINE"
else
  err "speccmds.cmd missing at $RUN_DIR"; exit 1
fi

# -------- Run drmemtrace + rwstats (online) --------
pushd "$RUN_DIR" >/dev/null
IPC_NAME="/tmp/drpipe.${SLURM_JOB_ID:-$$}.${SLURM_ARRAY_TASK_ID:-0}.${SHORT}"

DR_LAUNCH_OPTS=()
[[ "$DR_DEBUG" != "0" ]] && DR_LAUNCH_OPTS=( -debug -verbose 2 )

TRACER_OPTS=( -root "$DR_HOME" -follow_children -t drmemtrace -ipc_name "$IPC_NAME" -tool rwstats )

if [[ "$CAP_MODE" == "instr" ]]; then
  TRACER_OPTS+=( -trace_after_instrs $((WARMUP_M*1000000)) -trace_for_instrs $((ROI*1000000)) )
  APP_CMD=( /bin/bash -lc "cd '$RUN_CWD' && ${CMD_LINE}" )
elif [[ "$CAP_MODE" == "time" ]]; then
  if command -v timeout >/dev/null; then
    APP_CMD=( /bin/bash -lc "cd '$RUN_CWD' && /usr/bin/timeout ${TRACE_SEC}s ${CMD_LINE}" )
  else
    warn "'timeout' not available; running without time cap"
    APP_CMD=( /bin/bash -lc "cd '$RUN_CWD' && ${CMD_LINE}" )
  fi
else
  err "Unknown CAP_MODE='$CAP_MODE' (use 'instr' or 'time')"; exit 2
fi

set +e
"$DR_HOME/bin64/drrun" "${DR_LAUNCH_OPTS[@]}" "${TRACER_OPTS[@]}" -- "${APP_CMD[@]}" \
  1>"$RAW_DIR/runner.stdout.log" 2>"$DRR_LOG"
RC=$?
set -e
popd >/dev/null

# -------- Summary --------
echo "---- Online trace summary ----"
echo "Bench      : $BENCH"
echo "Mode       : $CAP_MODE"
[[ "$CAP_MODE" == "instr" ]] && echo "Warmup/ROI : ${WARMUP_M}M / ${ROI}M instr"
[[ "$CAP_MODE" == "time"  ]] && echo "Timeout    : ${TRACE_SEC}s"
echo "Run dir    : $RUN_DIR"
echo "CWD        : $RUN_CWD"
echo "Command    : $CMD_LINE"
echo "Log (rwstats stderr): $DRR_LOG"
echo "Exit code  : ${RC:-0}"

