"""Dispatch one frozen, native-approved OpenMM equilibration."""
import _bootstrap
import argparse
import json
import os
from pathlib import Path
from covscreen import external_openmm


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['inspect', 'reserve', 'run', 'fail'])
    parser.add_argument('run_id')
    parser.add_argument('--digest')
    parser.add_argument('--name')
    parser.add_argument('--state')
    args = parser.parse_args()
    root = Path(os.environ.get('COVSCREEN_PROJECT_ROOT', str(_bootstrap.PROJECT))).resolve()
    if args.action == 'inspect':
        result = external_openmm.inspect(root, args.run_id)
    elif args.action == 'reserve':
        result = external_openmm.reserve(root, args.run_id, args.digest)
    elif args.action == 'fail':
        result = external_openmm.fail(root, args.run_id, args.digest, args.name, args.state)
    else:
        result = external_openmm.run(root, args.run_id, args.digest)
        print(json.dumps({'run_id': result.run_id, 'state': result.state}))
        return 0 if result.state == 'COMPLETED' else 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
