"""Remote endpoint for the bounded Ligand-first HPC bridge (no credentials)."""

import _bootstrap  # canonical route and shared runtime, independent of cwd
import argparse
import json
from pathlib import Path
import sys

import os
ROOT = Path(os.environ.get("COVSCREEN_PROJECT_ROOT", str(_bootstrap.PROJECT))).resolve()
sys.path.insert(0, str(ROOT / "src"))
from covscreen import external_hpc

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["pending", "inspect", "reserve", "run", "run-instance", "inspect-instance", "fail", "cancel-inspect"])
parser.add_argument("run_id", nargs="?")
parser.add_argument("--digest")
parser.add_argument("--name")
parser.add_argument("--state")
args = parser.parse_args()
if args.action == "pending":
    result = external_hpc.pending(ROOT)
elif args.action == "inspect":
    result = external_hpc.inspect(ROOT, args.run_id)
elif args.action == "inspect-instance":
    from covscreen.instance_claim import inspect
    result = inspect(ROOT, args.run_id, "asgbie3")
elif args.action == "cancel-inspect":
    from covscreen.jobs import ArtifactRegistry
    from covscreen.scheduler_cancellation import read_intent
    result = external_hpc.inspect(ROOT, args.run_id)
    intent = read_intent(ArtifactRegistry(ROOT), args.run_id)
    result['cancellation'] = {'scope': intent['scope'], 'requested_at': intent['requested_at'],
                              'termination_confirmed': False}
elif args.action == "reserve":
    result = external_hpc.reserve(ROOT, args.run_id, args.digest)
elif args.action == "fail":
    result = external_hpc.fail(ROOT, args.run_id, args.digest, args.name, args.state)
elif args.action in {"run", "run-instance"}:
    if args.action == "run-instance":
        from covscreen.instance_claim import execute
        result = execute(ROOT, args.run_id, args.digest, "asgbie3")
    else:
        result = external_hpc.run(ROOT, args.run_id, args.digest)
    print(json.dumps({"run_id": result.run_id, "state": result.state}))
    raise SystemExit(0 if result.state == "COMPLETED" else 1)
print(json.dumps(result))
