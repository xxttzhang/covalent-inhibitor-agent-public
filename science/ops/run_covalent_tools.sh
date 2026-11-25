#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The login image exposes unrelated Schrödinger bypass variables. They must
# never enter the open-source tool process or a licensed Schrödinger command.
unset LD_PRELOAD SCHRODINGER SCHROD_LICENSE_FILE SCHRODINGER_LICENSE_FILE
unset LM_LICENSE_FILE PYTHONHOME

cd "$PROJECT_ROOT"
source environment/activate_carsidock.sh >/dev/null
exec python -u scripts/openscience_mcp_server.py
