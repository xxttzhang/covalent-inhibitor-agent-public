"""Dispatch native Ligand LO to its explicitly approved CPU instance."""
import argparse
import json
from pathlib import Path

import dispatch_ligand_hpc as hpc
from dispatch_ligand_vs import status as instance_status


def submit(run_id, path):
    if path.exists() or path.is_symlink():
        raise ValueError('LO receipt exists; observe the original task without replay')
    if not hpc.RELEASE_BINDING:
        raise ValueError('LO instance dispatch requires an immutable release/data binding')
    record = hpc.remote(['inspect-instance', run_id])
    if (record.get('engine') != 'schrodinger-covdock-lo' or record.get('run_id') != run_id
            or record.get('execution_instance') != hpc.ligand_instance()
            or record.get('state') != 'QUEUED' or record.get('worker_claimed') is not False):
        raise ValueError('LO dispatch requires the exact signed unclaimed instance task')
    record['release_binding'] = hpc.RELEASE_BINDING
    with path.open('x') as stream:
        path.chmod(0o600)
        json.dump(record, stream)
    result = hpc.remote(['launch-instance', run_id, '--digest', record['spec_sha256']])
    if result != {'submitted': True, 'name': hpc.ligand_instance()}:
        raise ValueError('Uncertain LO launch response; inspect the saved task, never replay')
    return result


def status(run_id, path):
    saved = json.loads(path.read_text())
    if saved.get('engine') != 'schrodinger-covdock-lo' or saved.get('execution_instance') != hpc.ligand_instance():
        raise ValueError('LO instance receipt has another engine or executor')
    return instance_status(run_id, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['submit', 'status'])
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--root', required=True)
    parser.add_argument('--binding', required=True, type=Path)
    args = parser.parse_args()
    if args.state.is_symlink():
        raise ValueError('LO dispatch receipt cannot be a symbolic link')
    saved = json.loads(args.state.read_text()) if args.action == 'status' else None
    hpc.configure_root(args.root, saved, required=True)
    hpc.configure_release_binding(json.loads(args.binding.read_text()))
    if saved:
        hpc.configure_release_binding(saved.get('release_binding'), saved)
    print(json.dumps(submit(args.run_id, args.state) if args.action == 'submit' else status(args.run_id, args.state)))


if __name__ == '__main__':
    main()
