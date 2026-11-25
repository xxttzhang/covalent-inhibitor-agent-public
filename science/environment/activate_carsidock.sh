#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SHARED_ROOT="${COVSCREEN_RUNTIME_ROOT:?Set COVSCREEN_RUNTIME_ROOT to the installed shared Python and CarsiDock runtime directory}"
export COVSCREEN_LEGACY_SHARED_ROOT="${COVSCREEN_LEGACY_SHARED_ROOT:-$(dirname "$SHARED_ROOT")}"
CARSI_ENV="$SHARED_ROOT/carsidock_gpu_env"
CARSI_RUNTIME="$SHARED_ROOT/python311_runtime"
CARSI_PYTHON="$CARSI_RUNTIME/bin/python"

if [[ ! -x "$CARSI_PYTHON" ]]; then
  echo "CarsiDock environment is not executable: $CARSI_PYTHON" >&2
  echo "Expected the standalone shared Python runtime." >&2
  return 1 2>/dev/null || exit 1
fi

# Never allow Schrödinger or preload libraries to leak into the open-source
# RDKit/PyArrow/PyTorch process.
unset SCHRODINGER SCHROD_LICENSE_FILE SCHRODINGER_LICENSE_FILE
unset LM_LICENSE_FILE PYTHONHOME LD_PRELOAD LD_AUDIT

# The legacy venv's pyvenv.cfg still names /opt/conda, which exists in GPU
# images but belongs to a different Python build. Do not activate that venv
# or modify it for other consumers: use the matching relocated interpreter
# and standard library, with only the registered dependency directory.
export PATH="$CARSI_RUNTIME/bin:$PATH"
export PYTHONHOME="$CARSI_RUNTIME"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
unset VIRTUAL_ENV

# The relocated Python 3.11 runtime ships ICU 78, which requires the matching
# GCC runtime (CXXABI_1.3.15). Keep its libstdc++ ahead of Ubuntu's 6.0.30.
export LD_LIBRARY_PATH="$SHARED_ROOT/python311_runtime/lib:$SHARED_ROOT/carsidock_runtime_libs/lib:/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu"
export PYTHONPATH="$PROJECT_ROOT/../routes/library-first/src:$PROJECT_ROOT/src:$CARSI_ENV/lib/python3.11/site-packages"
export DGLBACKEND=pytorch
export PYTHONUNBUFFERED=1

echo "Activated CarsiDock environment at $CARSI_ENV"
echo "Project root: $PROJECT_ROOT"
