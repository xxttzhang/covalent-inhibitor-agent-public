#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "Usage: bash scripts/run_egfr_covdock_lo_partition.sh PARTITION [NJOBS]" >&2
  echo "Example: bash scripts/run_egfr_covdock_lo_partition.sh 0 24" >&2
  exit 2
fi

PARTITION="$1"
NJOBS="${2:-24}"

PROJECT_DIR="${PROJECT_DIR:?Set PROJECT_DIR in the external deployment environment}"
SCHRODINGER_ROOT="${SCHRODINGER_ROOT:?Set SCHRODINGER_ROOT in the external deployment environment}"
SCHROD_LICENSE_FILE="${SCHROD_LICENSE_FILE:?Set SCHROD_LICENSE_FILE in the external deployment environment}"
PYTHON_BIN="${PYTHON_BIN:?Set PYTHON_BIN in the external deployment environment}"
COVDOCK_LO_HOST="${COVDOCK_LO_HOST:-localhost:${NJOBS}}"

case "$PARTITION" in
  0)
    LIGAND="data/processed/EGFR/covdock_lo/partitions/egfr_mini_0_139.mae"
    OUTDIR="results/EGFR-mini/covdock_lo_egfr_mini_p0"
    JOBNAME="EGFR_covdock_lo_p0"
    ;;
  1)
    LIGAND="data/processed/EGFR/covdock_lo/partitions/egfr_mini_140_279.mae"
    OUTDIR="results/EGFR-mini/covdock_lo_egfr_mini_p1"
    JOBNAME="EGFR_covdock_lo_p1"
    ;;
  2)
    LIGAND="data/processed/EGFR/covdock_lo/partitions/egfr_mini_280_419.mae"
    OUTDIR="results/EGFR-mini/covdock_lo_egfr_mini_p2"
    JOBNAME="EGFR_covdock_lo_p2"
    ;;
  *)
    echo "PARTITION must be 0, 1, or 2; got: $PARTITION" >&2
    exit 2
    ;;
esac

GRID_FILE="results/EGFR-mini/covdock_lo_grid_seed/EGFR_covdock_lo_grid_seed_v2_00_glidegrid-out.zip"
RECEPTOR="results/EGFR-mini/covdock_lo_grid_seed/EGFR_covdock_lo_grid_seed_receptor_prepared.mae"

cd "$PROJECT_DIR"

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export SCHRODINGER="$SCHRODINGER_ROOT"
export SCHROD_LICENSE_FILE
export USER="${USER:-$(id -un 2>/dev/null || echo root)}"
export LOGNAME="${LOGNAME:-$USER}"
unset LD_PRELOAD
unset LD_LIBRARY_PATH
unset PYTHONHOME

if [ ! -f "$SCHROD_LICENSE_FILE" ]; then
  echo "Missing official Schrödinger license file: $SCHROD_LICENSE_FILE" >&2
  exit 3
fi
if [ ! -f "$LIGAND" ]; then
  echo "Missing ligand partition: $LIGAND" >&2
  exit 3
fi
if [ ! -f "$GRID_FILE" ]; then
  echo "Missing CovDock grid: $GRID_FILE" >&2
  exit 3
fi
if [ ! -f "$RECEPTOR" ]; then
  echo "Missing prepared receptor: $RECEPTOR" >&2
  exit 3
fi

EXTRA_FLAGS=()
if [ "${COVDOCK_LO_AFFINITY:-1}" = "1" ]; then
  EXTRA_FLAGS+=(--covdock-flag affinity)
fi
if [ "${COVDOCK_LO_STRAIN:-1}" = "1" ]; then
  EXTRA_FLAGS+=(--covdock-flag strain)
fi
if [ "${COVDOCK_LO_MMGBSA:-0}" = "1" ]; then
  EXTRA_FLAGS+=(--covdock-flag mmgbsa)
fi
if [ -n "${COVDOCK_LO_MIN_RADIUS:-}" ]; then
  EXTRA_FLAGS+=(--min-radius "$COVDOCK_LO_MIN_RADIUS")
fi
if [ "${COVDOCK_LO_RESTART:-0}" = "1" ]; then
  EXTRA_FLAGS+=(--restart)
fi

mkdir -p "$OUTDIR"

"$PYTHON_BIN" scripts/run_covdock_job.py \
  --schrodinger-root "$SCHRODINGER_ROOT" \
  --receptor "$RECEPTOR" \
  --ligand "$LIGAND" \
  --attachment-residue A:797 \
  --reaction-type "Michael Addition" \
  --mode leadopt \
  --jobname "$JOBNAME" \
  --output-dir "$OUTDIR" \
  --njobs "$NJOBS" \
  --host "$COVDOCK_LO_HOST" \
  --grid-file "$GRID_FILE" \
  "${EXTRA_FLAGS[@]}"
