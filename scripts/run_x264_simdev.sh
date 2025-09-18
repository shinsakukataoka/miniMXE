# scripts/run_x264_simdev.sh
#!/usr/bin/env bash
set -euo pipefail
PARSECDIR=${PARSECDIR:-$HOME/benchmarks/parsec-3.0}
X264="$PARSECDIR/pkgs/apps/x264/inst/amd64-linux.gcc-pthreads/bin/x264"
IN="$PARSECDIR/pkgs/apps/x264/inputs/eledream_64x36_3.y4m"
OUT="$HOME/tmp/eledream.264"
mkdir -p "$(dirname "$OUT")"
exec "$X264" \
  --quiet --qp 20 --partitions b8x8,i4x4 --ref 5 --direct auto --b-pyramid --weightb \
  --mixed-refs --no-fast-pskip --me umh --subme 7 --analyse b8x8,i4x4 --threads 4 \
  -o "$OUT" "$IN"

