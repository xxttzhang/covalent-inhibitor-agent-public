"""Remote endpoint for explicitly selected native-approved smoke jobs."""

import _bootstrap  # canonical route and shared runtime, independent of cwd
import argparse
import json
from pathlib import Path
import sys

import os
ROOT = Path(os.environ.get("COVSCREEN_PROJECT_ROOT", str(_bootstrap.PROJECT))).resolve()
sys.path.insert(0, str(ROOT / 'src'))
from covscreen import external_inspire


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inspect', 'reserve', 'run', 'fail', 'replace-scheduler'])
    parser.add_argument('run_id')
    parser.add_argument('--digest')
    parser.add_argument('--name')
    parser.add_argument('--state')
    args = parser.parse_args()
    if args.action == 'inspect':
        result = external_inspire.inspect(ROOT, args.run_id)
    elif args.action == 'reserve':
        result = external_inspire.reserve(ROOT, args.run_id, args.digest)
    elif args.action == 'fail':
        result = external_inspire.fail(ROOT, args.run_id, args.digest, args.name, args.state)
    elif args.action == 'replace-scheduler':
        result = external_inspire.replace_failed_scheduler(
            ROOT, args.run_id, args.digest, args.name, args.state
        )
    else:
        result = external_inspire.run(ROOT, args.run_id, args.digest)
        print(json.dumps({'run_id': result.run_id, 'state': result.state}))
        return 0 if result.state == 'COMPLETED' else 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
