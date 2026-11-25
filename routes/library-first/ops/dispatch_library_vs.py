"""One-shot native Library prescreen/VS/LO dispatch through the signed CPU bridge."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'ligand-first/ops'))
import dispatch_ligand_vs as bridge


def check(record):
    if record.get('route') != 'library-first' or record.get('engine') not in {'covindb-prescreen', 'schrodinger-covdock-vs', 'schrodinger-covdock-lo'}:
        raise ValueError('Library dispatcher accepts only native Library prescreen, VS or LO jobs')
    budget = record['budget']
    minutes = {'covindb-prescreen': 15, 'schrodinger-covdock-vs': 60, 'schrodinger-covdock-lo': 180}[record['engine']]
    if (type(budget.get('gpu_count')) is not int or budget['gpu_count'] != 0
        or type(budget.get('cpu_count')) is not int or budget['cpu_count'] != 4
        or budget.get('memory_gb') != 16 or budget.get('max_runtime_minutes') != minutes or record['minutes'] != minutes
        or type(budget.get('max_candidates')) is not int or not 1 <= budget['max_candidates'] <= 5
        or record['engine'] == 'covindb-prescreen' and budget['max_candidates'] != 5):
        raise ValueError('Library CPU budget differs from the registered pilot')
    if not bridge.hpc.RELEASE_BINDING:
        raise ValueError('Library dispatch requires an immutable release/data binding')
    return bridge.hpc.bind_record(record)


def submit(run_id, path, image, *, instance=None):
    if instance is not None:
        if instance != 'asgbie2' or not bridge.hpc.RELEASE_BINDING:
            raise ValueError('Library instance dispatch requires asgbie2 and a fixed release')
        if path.exists() or path.is_symlink():
            raise ValueError('Library submission record exists; observe without replay')
        record = bridge.hpc.remote(['inspect-instance', run_id], instance=instance, transport='jupyter')
        if (record['run_id'] != run_id or record['execution_instance'] != instance
                or record['state'] != 'QUEUED' or record['worker_claimed']):
            raise ValueError('Library instance dispatch requires the exact unclaimed queued task')
        record['release_binding'] = bridge.hpc.RELEASE_BINDING
        record['dispatch_root'] = bridge.hpc.ROOT
        with path.open('x') as stream:
            path.chmod(0o600)
            json.dump(record, stream)
        response = bridge.hpc.remote(['launch-instance', run_id, '--digest', record['spec_sha256']],
                                     instance=instance, transport='jupyter')
        if response != {'submitted': True, 'name': instance}:
            raise ValueError('Library instance launch response differs; observe without replay')
        return response
    return bridge.submit(run_id, path, image, check_record=check)


def status(run_id, path):
    saved = json.loads(path.read_text())
    if saved.get('execution_instance') == 'asgbie2':
        if (saved['run_id'] != run_id or saved['release_binding'] != bridge.hpc.RELEASE_BINDING
                or saved['dispatch_root'] != bridge.hpc.ROOT):
            raise ValueError('Library instance receipt binding changed')
        observed = bridge.hpc.remote(['inspect-instance', run_id], instance='asgbie2', transport='jupyter')
        for key in ('run_id', 'spec_sha256', 'execution_instance'):
            if observed[key] != saved[key]:
                raise ValueError('Library instance observation identity changed')
        bootstrap = observed.get('bootstrap', {})
        return {'name': 'asgbie2', 'platform_state': observed['state'],
            'scheduler_state': bootstrap['state'] if bootstrap.get('state') in {'LAUNCH_FAILED', 'LAUNCH_UNCERTAIN'} else observed['state'],
            'bootstrap': bootstrap, 'scientific_acceptance': False, 'md_ready': False}
    return bridge.status(run_id, path, check_record=check)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['submit', 'status'])
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--binding', type=Path, required=True)
    parser.add_argument('--image', default='slurm-gromacs:0.1.1-alpha.1')
    parser.add_argument('--instance', choices=['asgbie2'])
    args = parser.parse_args()
    if args.state.is_symlink():
        raise ValueError('Library dispatch receipt cannot be a symbolic link')
    saved = json.loads(args.state.read_text()) if args.action == 'status' else None
    bridge.hpc.configure_root(args.root, saved, required=True)
    bridge.hpc.configure_release_binding(json.loads(args.binding.read_text()), saved)
    print(json.dumps(submit(args.run_id, args.state, args.image, instance=args.instance) if args.action == 'submit' else status(args.run_id, args.state)))


if __name__ == '__main__':
    main()
