#!/usr/bin/env bash
# SPEC2017 build-if-needed + tiny runner makers
set -euo pipefail

SPEC_ROOT="${SPEC_ROOT:-$HOME/spec2017}"
GCC_DIR="${GCC_DIR:-/cm/local/apps/gcc/13.1.0}"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <BENCH_ID> [<BENCH_ID> ...]"
  echo "  e.g. $0 531.deepsjeng_r 505.mcf_r"
  exit 1
fi

# --- enter SPEC and source env ---
cd "$SPEC_ROOT"
if [[ ! -f shrc ]]; then
  echo "[ERR] Can't find $SPEC_ROOT/shrc"; exit 1
fi
# shellcheck source=/dev/null
. ./shrc
command -v runcpu >/dev/null || { echo "[ERR] runcpu not in PATH after sourcing shrc"; exit 1; }

ensure_built_and_run() { # <BENCH>
  local BENCH="$1"
  local BDIR="$SPEC_ROOT/benchspec/CPU/$BENCH"
  local RDIR="$BDIR/run"

  echo
  echo "==== ${BENCH}: check build/run dirs ===="

  # Heuristic: consider "built" if a run_* dir has a *_base.* binary
  local has_binary=0
  if [[ -d "$RDIR" ]] && ls -dt "$RDIR"/run_* >/dev/null 2>&1; then
    local latest
    latest="$(ls -dt "$RDIR"/run_* | head -1)"
    if find "$latest" -maxdepth 2 -type f -name '*_base.*' | grep -q .; then
      has_binary=1
    fi
  fi

  if [[ $has_binary -eq 1 ]]; then
    echo "[OK ] Build already present — skipping build."
  else
    echo "[.. ] Building + running test size once to materialize run dir…"
    runcpu --config my-gcc.cfg --define "gcc_dir=${GCC_DIR}" \
           --tune base --size test --action build "$BENCH"
    runcpu --config my-gcc.cfg --define "gcc_dir=${GCC_DIR}" \
           --tune base --size test --action run   "$BENCH"
  fi

  # Always (re)find newest run dir
  [[ -d "$RDIR" ]] || { echo "[ERR] No run dir for $BENCH"; return 1; }
  local run_latest
  run_latest="$(ls -dt "$RDIR"/run_* | head -1)" || true
  [[ -n "${run_latest:-}" && -d "$run_latest" ]] || { echo "[ERR] No run_* for $BENCH"; return 1; }

  echo "[OK ] Newest run dir: $run_latest"

  # Pull the binary and args
  local BIN=""
  BIN="$(find "$run_latest" -maxdepth 2 -type f -name '*_base.*' | sort | head -1 || true)"
  [[ -n "$BIN" ]] || { echo "[ERR] *_base.* binary not found in $run_latest"; return 1; }

  local ARGS=""
  if [[ -f "$run_latest/speccmds.cmd" ]]; then
    local LINE LINE_TRIM
    LINE="$(grep -m1 -E '../run_base[^ ]+/[^ ]+_base[^ ]+|./[^ ]+_base[^ ]+' "$run_latest/speccmds.cmd" || true)"
    if [[ -n "$LINE" ]]; then
      LINE_TRIM="${LINE%%>*}"
      # Everything after the binary path is the arg string
      ARGS="$(echo "$LINE_TRIM" | sed -E 's@.*_base[^ ]+[[:space:]]*(.*)$@\1@' | xargs || true)"
    fi
  fi

  # Special fallbacks for common SPEC tests
  [[ -z "$ARGS" && -f "$run_latest/test.txt" ]] && ARGS="test.txt"
  [[ -z "$ARGS" && -f "$run_latest/test.sgf" ]] && ARGS="test.sgf"
  if [[ "$BENCH" == "648.exchange2_s" && -z "${ARGS:-}" ]]; then ARGS="2"; fi

  echo "[OK ] Binary : $BIN"
  echo "[OK ] Args   : ${ARGS:-<none>}"

  # Create tiny runners inside the newest run dir
  cat > "$run_latest/echo_cmd.sh" <<'EOS'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
BIN="$(find . -maxdepth 2 -type f -name '*_base.*' | sort | head -1)"
if [[ -z "${BIN:-}" ]]; then echo "[ERR] no _base binary"; exit 1; fi
# Try to reconstruct ARGS similarly to the parent script
ARGS=""
if [[ -f "speccmds.cmd" ]]; then
  LINE="$(grep -m1 -E '../run_base[^ ]+/[^ ]+_base[^ ]+|./[^ ]+_base[^ ]+' "speccmds.cmd" || true)"
  if [[ -n "${LINE:-}" ]]; then
    LINE_TRIM="${LINE%%>*}"
    ARGS="$(echo "$LINE_TRIM" | sed -E 's@.*_base[^ ]+[[:space:]]*(.*)$@\1@' | xargs || true)"
  fi
fi
[[ -z "${ARGS:-}" && -f "test.txt" ]] && ARGS="test.txt"
[[ -z "${ARGS:-}" && -f "test.sgf" ]] && ARGS="test.sgf"
echo "./$(basename "$BIN") ${ARGS:-}"
EOS
  chmod +x "$run_latest/echo_cmd.sh"

  cat > "$run_latest/run_native.sh" <<'EOS'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
BIN="$(find . -maxdepth 2 -type f -name '*_base.*' | sort | head -1)"
if [[ -z "${BIN:-}" ]]; then echo "[ERR] no _base binary"; exit 1; fi
ARGS=""
if [[ -f "speccmds.cmd" ]]; then
  LINE="$(grep -m1 -E '../run_base[^ ]+/[^ ]+_base[^ ]+|./[^ ]+_base[^ ]+' "speccmds.cmd" || true)"
  if [[ -n "${LINE:-}" ]]; then
    LINE_TRIM="${LINE%%>*}"
    ARGS="$(echo "$LINE_TRIM" | sed -E 's@.*_base[^ ]+[[:space:]]*(.*)$@\1@' | xargs || true)"
  fi
fi
[[ -z "${ARGS:-}" && -f "test.txt" ]] && ARGS="test.txt"
[[ -z "${ARGS:-}" && -f "test.sgf" ]] && ARGS="test.sgf"
exec "./$(basename "$BIN")" ${ARGS:+$ARGS}
EOS
  chmod +x "$run_latest/run_native.sh"

  echo "[OK ] Wrote: $run_latest/echo_cmd.sh"
  echo "[OK ] Wrote: $run_latest/run_native.sh"
}

for B in "$@"; do
  ensure_built_and_run "$B"
done

echo
echo "[DONE] All requested benchmarks processed."

