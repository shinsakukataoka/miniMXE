#!/usr/bin/env bash
set -euo pipefail

# Usage: bash run_energy_and_cat.sh test_all_spec_benchmarks20250920T204859Z
if [ $# -ne 1 ]; then
  echo "Usage: $0 <RUN_ID>"
  exit 1
fi

RUN_ID="$1"
ROOT="/home/skataoka26/COSC_498/miniMXE"
RESULTS="/home/skataoka26/COSC_498/miniMXE/results/${RUN_ID}"
PY_SCRIPT="/home/skataoka26/COSC_498/miniMXE/scripts/energy_ed2p_v3.py"

# --- Phase 1: run python for each SRAM/JanS pair ---
for SRAM_DIR in "${RESULTS}"/*_sram_*; do
  [ -d "${SRAM_DIR}" ] || continue
  [ -f "${SRAM_DIR}/sim.out" ] || continue

  # bench prefix (e.g., 520_omnetpp_r)
  BN="$(basename "${SRAM_DIR}")"
  PREFIX="$(echo "${BN}" | sed -E 's/_sram_.*//')"

  # find matching JanS directory (any variant like JanS_cap_approx)
  JANS_DIR="$(ls -d "${RESULTS}/${PREFIX}"*JanS* 2>/dev/null | head -n 1 || true)"
  if [ -z "${JANS_DIR}" ] || [ ! -f "${JANS_DIR}/sim.out" ]; then
    echo "[SKIP] Missing JanS for ${PREFIX}"
    continue
  fi

  # absolute-path run
  python3 "/home/skataoka26/COSC_498/miniMXE/scripts/energy_ed2p_v3.py" \
          "${SRAM_DIR}/sim.out" \
          "${JANS_DIR}/sim.out"
done

# --- Phase 2: cat all CSVs from each output_* directory ---
for OUT_DIR in "${RESULTS}"/output_*; do
  [ -d "${OUT_DIR}" ] || continue
  echo "===== $(basename "${OUT_DIR}") ====="
  cat "${OUT_DIR}/energy_bounds.csv"
  cat "${OUT_DIR}/summary.csv"
done

