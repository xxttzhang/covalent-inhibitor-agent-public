#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "Usage: bash scripts/run_covdock_lo_partition.sh PARTITION [NJOBS]" >&2
  echo "Example: bash scripts/run_covdock_lo_partition.sh 0 24" >&2
  exit 2
fi

PARTITION="$1"
NJOBS="${2:-24}"

PROJECT_DIR="${PROJECT_DIR:?Set PROJECT_DIR in the external deployment environment}"
SCHRODINGER_ROOT="${SCHRODINGER_ROOT:?Set SCHRODINGER_ROOT in the external deployment environment}"
SCHROD_LICENSE_FILE="${SCHROD_LICENSE_FILE:?Set SCHROD_LICENSE_FILE in the external deployment environment}"
PYTHON_BIN="${PYTHON_BIN:?Set PYTHON_BIN in the external deployment environment}"
COVDOCK_LO_HOST="${COVDOCK_LO_HOST:-localhost:${NJOBS}}"

cd "$PROJECT_DIR"

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export SCHRODINGER="$SCHRODINGER_ROOT"
export SCHROD_LICENSE_FILE
export USER="${USER:-$(id -un 2>/dev/null || echo root)}"
export LOGNAME="${LOGNAME:-$USER}"

LIGAND="${COVDOCK_LO_LIGAND:-data/processed/BTK/covdock_grid_split/four/covdock_btk_mini_q${PARTITION}.mae}"
GRID_FILE="data/processed/BTK/covdock_grid_split/BTK_covdock_btk_mini_00_glidegrid-out.zip"
RECEPTOR="results/BTK-mini/covdock_smoke_official/BTK_B1852_covdock_smoke_official_receptor_prepared.mae"
OUTDIR="${COVDOCK_LO_OUTDIR:-results/BTK-mini/covdock_lo_btk_mini_q${PARTITION}}"
JOBNAME="${COVDOCK_LO_JOBNAME:-BTK_covdock_lo_btk_mini_q${PARTITION}}"

if [ ! -f "$LIGAND" ]; then
  echo "Missing ligand partition: $LIGAND" >&2
  exit 3
fi
if [ ! -f "$GRID_FILE" ]; then
  echo "Missing CovDock grid: $GRID_FILE" >&2
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
  --attachment-residue A:481 \
  --reaction-type "Michael Addition" \
  --mode leadopt \
  --jobname "$JOBNAME" \
  --output-dir "$OUTDIR" \
  --njobs "$NJOBS" \
  --host "$COVDOCK_LO_HOST" \
  --grid-file "$GRID_FILE" \
  "${EXTRA_FLAGS[@]}"
