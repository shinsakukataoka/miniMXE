#!/usr/bin/env bash
set -euo pipefail

# ------- settings -------
CYCLE_NS="${CYCLE_NS:-0.37594}"   # core period in ns
PROJ="/home/skataoka26/COSC_498"
NV_BASE="$PROJ/devices/results/cache_triplet"
OUT_DIR="$PROJ/miniMXE/config"
mkdir -p "$OUT_DIR"

ceil_cycles() { awk -v ns="$1" -v T="$CYCLE_NS" 'BEGIN{v=ns/T; c=int(v); if(v>c)c++; if(c<1)c=1; print c}'; }
to_pj()       { awk -v nj="$1" 'BEGIN{printf "%d", nj*1000 + 0.5}'; }
mb2bytes()    { awk -v m="$1"  'BEGIN{printf "%d", m*1024*1024}'; }

# Buckets keyed by "<cap>mb_<assoc>w"
declare -A RNS_sram WNS_sram ERNJ_sram EWNJ_sram LMW_sram LINE_sram TEMP_sram TECH_sram BUSW_sram SRC_sram
declare -A RNS_jans WNS_jans ERNJ_jans EWNJ_jans LMW_jans LINE_jans TEMP_jans TECH_jans BUSW_jans SRC_jans
declare -A CAPMB ASSOC

parse_file() {
  local f="$1"
  case "$f" in
    */SRAM/*)   dev="sram" ;;
    */STTRAM/*) dev="jans" ;;     # Jans == MRAM
    */eDRAM1T/*) return 0 ;;      # skip eDRAM entirely
    *) return 0 ;;
  esac

  # basics (prefer in-file, fallback to path)
  local cap_mb assoc lineB tempK technm busw
  cap_mb="$(awk -F': *' '/^Capacity:/{gsub(/MB/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"
  [[ -z "$cap_mb" ]] && cap_mb="$(sed -n 's#.*/C\([0-9]\+\)MB_.*#\1#p' <<<"$f")"
  assoc="$(awk -F': *' '/^Cache Associativity:/{gsub(/ Ways/,"",$2); print $2; exit}' "$f")"
  [[ -z "$assoc" ]] && assoc="$(sed -n 's#.*/A\([0-9]\+\)_.*#\1#p' <<<"$f")"
  lineB="$(awk -F': *' '/^Cache Line Size:/{gsub(/Bytes/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"; [[ -z "$lineB" ]] && lineB=64
  tempK="$(awk -F': *' '/^Temperature:/{gsub(/K/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"
  [[ -z "$tempK" ]] && tempK="$(sed -n 's#.*_T\([0-9]\+\)_.*#\1#p' <<<"$f")"
  technm="$(awk -F': *' '/^Peripheral Node:/{gsub(/nm/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"; [[ -z "$technm" ]] && technm=32
  busw="$(sed -n 's#.*_W\([0-9]\+\)_.*#\1#p' <<<"$f")"; [[ -z "$busw" ]] && busw=512

  # summary metrics (require all)
  local read_ns write_ns eread_nj ewrite_nj leak_mw
  read_ns="$(awk -F'= *' '/Cache Hit Latency/{gsub(/ns.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")" || true
  write_ns="$(awk -F'= *' '/Cache Write Latency/{gsub(/ns.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")" || true
  eread_nj="$(awk -F'= *' '/Cache Hit Dynamic Energy/{gsub(/nJ.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")" || true
  ewrite_nj="$(awk -F'= *' '/Cache Write Dynamic Energy/{gsub(/nJ.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")" || true
  leak_mw="$(awk -F'= *' '/Cache Total Leakage Power/{gsub(/mW.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")" || true
  [[ -z "${read_ns:-}" || -z "${write_ns:-}" || -z "${eread_nj:-}" || -z "${ewrite_nj:-}" || -z "${leak_mw:-}" ]] && { echo "SKIP (no valid summary): $f"; return; }

  local key="${cap_mb}mb_${assoc}w"
  CAPMB["$key"]="$cap_mb"; ASSOC["$key"]="$assoc"

  if [[ "$dev" == "sram" ]]; then
    RNS_sram["$key"]="$read_ns";   WNS_sram["$key"]="$write_ns"
    ERNJ_sram["$key"]="$eread_nj"; EWNJ_sram["$key"]="$ewrite_nj"; LMW_sram["$key"]="$leak_mw"
    LINE_sram["$key"]="$lineB";    TEMP_sram["$key"]="$tempK";     TECH_sram["$key"]="$technm"; BUSW_sram["$key"]="$busw"
    SRC_sram["$key"]="$f"
  else
    RNS_jans["$key"]="$read_ns";   WNS_jans["$key"]="$write_ns"
    ERNJ_jans["$key"]="$eread_nj"; EWNJ_jans["$key"]="$ewrite_nj"; LMW_jans["$key"]="$leak_mw"
    LINE_jans["$key"]="$lineB";    TEMP_jans["$key"]="$tempK";     TECH_jans["$key"]="$technm"; BUSW_jans["$key"]="$busw"
    SRC_jans["$key"]="$f"
  fi
}

# Parse all NVSim stdout files (SRAM + STTRAM only)
while IFS= read -r -d '' f; do parse_file "$f"; done < <(find "$NV_BASE" -type f -name '*.stdout.txt' -print0)

# Emit one YAML per (cap,assoc) that has BOTH SRAM and Jans
for key in "${!CAPMB[@]}"; do
  [[ -z "${RNS_sram[$key]:-}" || -z "${RNS_jans[$key]:-}" ]] && { echo "WARN: missing pair for $key; skipping."; continue; }

  cap="${CAPMB[$key]}"; assoc="${ASSOC[$key]}"; size_bytes="$(mb2bytes "$cap")"
  out="$OUT_DIR/sniper_${cap}mb_${assoc}w_nvsim.yaml"

  # SRAM numbers
  sram_r_cyc="$(ceil_cycles "${RNS_sram[$key]}")"
  sram_w_cyc="$(ceil_cycles "${WNS_sram[$key]}")"
  sram_e_r="$(to_pj "${ERNJ_sram[$key]}")"
  sram_e_w="$(to_pj "${EWNJ_sram[$key]}")"

  # Jans numbers
  jans_r_cyc="$(ceil_cycles "${RNS_jans[$key]}")"
  jans_w_cyc="$(ceil_cycles "${WNS_jans[$key]}")"
  jans_e_r="$(to_pj "${ERNJ_jans[$key]}")"
  jans_e_w="$(to_pj "${EWNJ_jans[$key]}")"

  cat > "$out" <<YAML
# Auto-generated from NVSim pairs (SRAM + JanS) for ${cap}MB, ${assoc}-way
script: scripts/run_all.sbatch
sbatch_args:
  - --job-name=sniper-llc-${cap}mb-${assoc}w
  - --output=logs/%x-%A_%a.out
  - --error=logs/%x-%A_%a.err

env:
  SKIP_TRACE: 1
  SPEC_SIZE: ref
  SIM_N: 4
  ROI_M: 1000
  WARMUP_M: 100
  OUT_ROOT: "\${PWD}/results/sniper_llc_${cap}mb_${assoc}w_{timestamp}"

llc:
  sram:
    # ---- hard-coded from NVSim ----
    size_bytes: ${size_bytes}           # ${cap} MB
    assoc_ways: ${assoc}
    line_bytes: ${LINE_sram[$key]}
    bus_width_bits: ${BUSW_sram[$key]}
    temp_k: ${TEMP_sram[$key]}
    tech_node_nm: ${TECH_sram[$key]}
    read_hit_cycles: ${sram_r_cyc}      # ceil(${RNS_sram[$key]} ns / ${CYCLE_NS} ns)
    write_hit_cycles: ${sram_w_cyc}     # ceil(${WNS_sram[$key]} ns / ${CYCLE_NS} ns)
    energy:
      enabled: true
      e_read_hit_pJ: ${sram_e_r}        # ${ERNJ_sram[$key]} nJ
      e_write_hit_pJ: ${sram_e_w}       # ${EWNJ_sram[$key]} nJ
      e_miss_pJ: ${sram_e_r}            # NVSim miss ~= hit
      p_leak_mW: ${LMW_sram[$key]}

  jans:
    # ---- hard-coded from NVSim STTRAM ----
    size_bytes: ${size_bytes}           # ${cap} MB
    assoc_ways: ${assoc}
    line_bytes: ${LINE_jans[$key]}
    bus_width_bits: ${BUSW_jans[$key]}
    temp_k: ${TEMP_jans[$key]}
    tech_node_nm: ${TECH_jans[$key]}
    read_hit_cycles: ${jans_r_cyc}      # ceil(${RNS_jans[$key]} ns / ${CYCLE_NS} ns)
    write_hit_cycles: ${jans_w_cyc}     # ceil(${WNS_jans[$key]} ns / ${CYCLE_NS} ns)
    energy:
      enabled: true
      e_read_hit_pJ: ${jans_e_r}        # ${ERNJ_jans[$key]} nJ
      e_write_hit_pJ: ${jans_e_w}       # ${EWNJ_jans[$key]} nJ
      e_miss_pJ: ${jans_e_r}            # NVSim miss ~= hit
      p_leak_mW: ${LMW_jans[$key]}

notes:
  - Cycle period (ns): ${CYCLE_NS}
  - Source SRAM: ${SRC_sram[$key]}
  - Source JanS: ${SRC_jans[$key]}
YAML

  echo "Wrote: $out"
done
