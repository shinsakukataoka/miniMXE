# Use bash and keep each recipe in a single shell
SHELL := /bin/bash
.ONESHELL:
-include env.mk

# -------- Defaults (override in env.mk or on the CLI) --------
SPEC_ROOT        ?= $(HOME)/spec2017
DR_HOME          ?= $(HOME)/opt/DynamoRIO-Linux-11.3.0-1
SNIPER_HOME      ?= $(HOME)/src/sniper
GCC_DIR          ?= /cm/local/apps/gcc/13.1.0
CONDA_SQLITE_LIB ?= $(HOME)/miniconda3/lib
TMPDIR           ?= $(HOME)/tmp

OUT_ROOT     ?= $(CURDIR)/results
FEATURES_CSV ?= $(CURDIR)/features.csv

BENCH       ?= 531.deepsjeng_r
N_M         ?= 10
TRACE_SEC   ?= 4
FEATURES_M  ?= 10
BUILD_IF_NEEDED ?= 1

# JanS LLC overrides
JANS_L3_SIZE ?= 2097152
JANS_L3_ASSOC ?= 16
JANS_L3_LAT ?= 8

SHORT    := $(shell echo $(BENCH) | tr '.' '_')
OUT_SRAM := $(OUT_ROOT)/$(SHORT)_sram_$(N_M)M
OUT_JANS := $(OUT_ROOT)/$(SHORT)_JanS_cap_approx_$(N_M)M

.PHONY: help sanity run energy clean clean-results

help:
	@echo "Targets:"
	@echo "  make sanity                    # check tools & environment"
	@echo "  make run BENCH=... N_M=...     # run full pipeline"
	@echo "  make energy BENCH=... N_M=...  # recompute energy/ED^2P from sim.out"
	@echo "  make clean-results             # delete results only"
	@echo "  make clean                     # delete results + traces"

sanity:
	set -euo pipefail
	mkdir -p "$(OUT_ROOT)" "$(TMPDIR)" traces
	# Source SPEC shrc from inside SPEC_ROOT so it detects the correct tree
	if [[ -f "$(SPEC_ROOT)/shrc" ]]; then \
		pushd "$(SPEC_ROOT)" >/dev/null; \
		. ./shrc; \
		popd >/dev/null; \
	else \
		echo "[ERR] SPEC shrc not found at $(SPEC_ROOT)/shrc"; \
		exit 1; \
	fi
	"$(DR_HOME)/bin64/drrun" -version >/dev/null && echo "[OK] DynamoRIO" || { echo "[ERR] DynamoRIO"; exit 1; }
	command -v runcpu >/dev/null && echo "[OK] SPEC runcpu" || { echo "[ERR] SPEC runcpu"; exit 1; }
	if ldd "$(SNIPER_HOME)/lib/sniper" 2>/dev/null | grep -qi sqlite; then \
		echo "[OK] sqlite visible to Sniper"; \
	else \
		echo "[WARN] sqlite not in Sniper link; using LD_LIBRARY_PATH=$(CONDA_SQLITE_LIB)"; \
	fi
	echo "[OK] Sanity checks complete."

run: sanity
	bash scripts/run_spec_pipeline.sh \
	  --bench "$(BENCH)" --n-m "$(N_M)" \
	  --spec-root "$(SPEC_ROOT)" --dr-home "$(DR_HOME)" --sniper-home "$(SNIPER_HOME)" \
	  --gcc-dir "$(GCC_DIR)" --conda-sqlite-lib "$(CONDA_SQLITE_LIB)" --tmpdir "$(TMPDIR)" \
	  --out-root "$(OUT_ROOT)" --features-csv "$(FEATURES_CSV)" \
	  --trace-sec "$(TRACE_SEC)" --features-M "$(FEATURES_M)" \
	  --jans-l3-size "$(JANS_L3_SIZE)" --jans-l3-assoc "$(JANS_L3_ASSOC)" --jans-l3-lat "$(JANS_L3_LAT)" \
	  $(if $(BUILD_IF_NEEDED),--build-if-needed,)

energy:
	python3 scripts/energy_ed2p.py \
	  "$(OUT_SRAM)/sim.out" "$(OUT_JANS)/sim.out"

clean-results:
	rm -rf "$(OUT_ROOT)"

clean: clean-results
	rm -rf traces

