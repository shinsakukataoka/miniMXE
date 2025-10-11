#!/usr/bin/env bash
set -euo pipefail

CYCLE_NS="${CYCLE_NS:-0.37594}"     # core period (ns)
PROJ="/home/skataoka26/COSC_498"
NV_BASE="$PROJ/devices/results/cache_triplet"
OUT_DIR="$PROJ/miniMXE/config"
mkdir -p "$OUT_DIR"

ceil_cycles(){ awk -v ns="$1" -v T="$CYCLE_NS" 'BEGIN{v=ns/T; c=int(v); print (v>c)?c+1:(c<1?1:1*c)}'; }
to_pj(){ awk -v nj="$1" 'BEGIN{printf "%d", nj*1000 + 0.5}'; }
mb2b(){ awk -v m="$1" 'BEGIN{printf "%d", m*1024*1024}'; }

process_file() {
  local f="$1" devname devkey
  case "$f" in
    */SRAM/*)    devname="sram";  devkey="sram"  ;;
    */STTRAM/*)  devname="mram";  devkey="jans"  ;; # your sim expects MRAM under "jans"
    */eDRAM1T/*) devname="edram"; devkey="edram" ;;
    *) echo "SKIP (unknown dev): $f"; return ;;
  esac

  # Pull basic params (prefer inside file, fall back to path)
  local cap_mb assoc lineB tempK technm busw
  cap_mb="$(awk -F': *' '/^Capacity:/{gsub(/MB/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"
  [[ -z "$cap_mb" ]] && cap_mb="$(sed -n 's#.*/C\([0-9]\+\)MB_.*#\1#p' <<<"$f")"
  assoc="$(awk -F': *' '/^Cache Associativity:/{gsub(/ Ways/,"",$2); print $2; exit}' "$f")"
  [[ -z "$assoc" ]] && assoc="$(sed -n 's#.*/A\([0-9]\+\)_.*#\1#p' <<<"$f")"
  lineB="$(awk -F': *' '/^Cache Line Size:/{gsub(/Bytes/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"
  [[ -z "$lineB" ]] && lineB=64
  tempK="$(awk -F': *' '/^Temperature:/{gsub(/K/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"
  [[ -z "$tempK" ]] && tempK="$(sed -n 's#.*_T\([0-9]\+\)_.*#\1#p' <<<"$f")"
  technm="$(awk -F': *' '/^Peripheral Node:/{gsub(/nm/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"
  [[ -z "$technm" ]] && technm=32
  busw="$(sed -n 's#.*_W\([0-9]\+\)_.*#\1#p' <<<"$f")"
  [[ -z "$busw" ]] && busw=512

  # Summary metrics
  local read_ns write_ns eread_nj ewrite_nj leak_mw
  read_ns="$(awk -F'= *' '/Cache Hit Latency/{gsub(/ns.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"
  write_ns="$(awk -F'= *' '/Cache Write Latency/{gsub(/ns.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"
  eread_nj="$(awk -F'= *' '/Cache Hit Dynamic Energy/{gsub(/nJ.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"
  ewrite_nj="$(awk -F'= *' '/Cache Write Dynamic Energy/{gsub(/nJ.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"
  leak_mw="$(awk -F'= *' '/Cache Total Leakage Power/{gsub(/mW.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")"

  # Skip files with no valid summary (eDRAM frequently)
  if [[ -z "${read_ns:-}" || -z "${write_ns:-}" || -z "${eread_nj:-}" || -z "${ewrite_nj:-}" || -z "${leak_mw:-}" ]]; then
    echo "SKIP (no valid summary): $f"
    return
  fi

  # eDRAM refresh details (if present)
  local ref_lat_us avail_pct ref_pwr_mw_per_bank retain_ns
  ref_lat_us="$(awk -F'= *' '/Cache Refresh Latency/{gsub(/us.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")" || true
  avail_pct="$(awk -F'= *' '/Cache Availability/{gsub(/%.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")" || true
  ref_pwr_mw_per_bank="$(awk -F'= *' '/Cache Refresh Power/{gsub(/mW.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")" || true
  retain_ns="$(awk -F'= *' '/Cache Retention Time/{gsub(/ns.*/,"",$2); gsub(/ /,"",$2); print $2; exit}' "$f")" || true

  local read_cyc write_cyc eread_pj ewrite_pj size_bytes
  read_cyc="$(ceil_cycles "$read_ns")"
  write_cyc="$(ceil_cycles "$write_ns")"
  eread_pj="$(to_pj "$eread_nj")"
  ewrite_pj="$(to_pj "$ewrite_nj")"
  size_bytes="$(mb2b "$cap_mb")"

  local fname="sniper_${devname}_${cap_mb}mb_${assoc}w_nvsim.yaml"
  local out="$OUT_DIR/$fname"

  {
    echo "# Auto-generated from NVSim: $(basename "$f")"
    echo "script: scripts/run_all.sbatch"
    echo "sbatch_args:"
    echo "  - --job-name=sniper-llc-${devname}-${cap_mb}mb-${assoc}w"
    echo "  - --output=logs/%x-%A_%a.out"
    echo "  - --error=logs/%x-%A_%a.err"
    echo "env:"
    echo "  SKIP_TRACE: 1"
    echo "  SPEC_SIZE: ref"
    echo "  SIM_N: 4"
    echo "  ROI_M: 1000"
    echo "  WARMUP_M: 100"
    echo "  OUT_ROOT: \"\${PWD}/results/sniper_llc_${devname}_${cap_mb}mb_${assoc}w_{timestamp}\""
    echo "llc:"
    echo "  ${devkey}:"
    echo "    # ---- hard-coded LLC parameters ----"
    echo "    size_bytes: ${size_bytes}        # ${cap_mb} MB"
    echo "    assoc_ways: ${assoc}             # NVSim \"Cache Associativity\""
    echo "    line_bytes: ${lineB}             # NVSim \"Cache Line Size\""
    echo "    bus_width_bits: ${busw}          # parsed from path (_W${busw}_)"
    echo "    temp_k: ${tempK}                 # NVSim Temperature"
    echo "    tech_node_nm: ${technm}          # NVSim Peripheral Node"
    echo "    read_hit_cycles: ${read_cyc}     # ceil(${read_ns} ns / ${CYCLE_NS} ns)"
    echo "    write_hit_cycles: ${write_cyc}   # ceil(${write_ns} ns / ${CYCLE_NS} ns)"
    echo "    energy:"
    echo "      enabled: true"
    echo "      e_read_hit_pJ: ${eread_pj}     # ${eread_nj} nJ"
    echo "      e_write_hit_pJ: ${ewrite_pj}   # ${ewrite_nj} nJ"
    echo "      e_miss_pJ: ${eread_pj}         # set equal to read-hit (NVSim)"
    echo "      p_leak_mW: ${leak_mw}"
    if [[ "$devname" == "edram" ]]; then
      echo "    refresh:"
      echo "      model_warning: \"NVSim eDRAM model under development\""
      [[ -n "${retain_ns:-}" ]]         && echo "      retention_time_ns: ${retain_ns}"
      [[ -n "${ref_lat_us:-}" ]]        && echo "      refresh_latency_us_per_bank: ${ref_lat_us}"
      [[ -n "${ref_pwr_mw_per_bank:-}" ]] && echo "      refresh_power_mw_per_bank: ${ref_pwr_mw_per_bank}"
      [[ -n "${avail_pct:-}" ]]         && echo "      availability_percent: ${avail_pct}"
    fi
    echo "notes:"
    echo "  - Cycle period (ns): ${CYCLE_NS}"
    echo "  - Source: ${f}"
  } > "$out"

  echo "Wrote: $out"
}

export -f process_file ceil_cycles to_pj mb2b
export CYCLE_NS OUT_DIR

find "$NV_BASE" -type f -name '*.stdout.txt' -print0 \
  | xargs -0 -I{} bash -c 'process_file "$@"' _ {}
