"""One-shot VS CPU dispatch from a signed, fixed-release job; no engine fallback."""
import argparse
import json
import os
from pathlib import Path

import dispatch_ligand_hpc as hpc


ENGINE = 'schrodinger-covdock-vs'


def check(record):
    if record.get('engine') != ENGINE or record.get('route') != 'ligand-first':
        raise ValueError('VS dispatcher accepts only native Ligand VS jobs')
    budget = record['budget']
    if (budget.get('gpu_count') != 0 or type(budget.get('gpu_count')) is not int
            or budget.get('cpu_count') != 4 or budget.get('memory_gb') != 16
            or budget.get('max_runtime_minutes') != 60 or record['minutes'] != 60
            or type(budget.get('max_candidates')) is not int or not 1 <= budget['max_candidates'] <= 3):
        raise ValueError('VS scheduler budget differs from the registered Ligand pilot')
    if not hpc.RELEASE_BINDING:
        raise ValueError('VS dispatch requires an explicit immutable release/data binding')
    return hpc.bind_record(record)


def submit(run_id, path, image, *, check_record=check):
    if path.exists() or path.is_symlink():
        raise ValueError('VS submission record exists; observe it without replay')
    if os.environ.get('COVALENT_LIGAND_VS_INSTANCE') in {'asgbie3', 'asgbie4'}:
        record = hpc.remote(['inspect-instance', run_id])
        if (record['run_id'] != run_id or record['execution_instance'] != hpc.ligand_instance()
                or record['state'] != 'QUEUED' or record['worker_claimed']):
            raise ValueError('Instance dispatch requires the exact unclaimed queued task')
        record['release_binding'] = hpc.RELEASE_BINDING
        with path.open('x') as stream:
            path.chmod(0o600)
            json.dump(record, stream)
        response = hpc.remote(['launch-instance', run_id, '--digest', record['spec_sha256']])
        if response != {'submitted': True, 'name': hpc.ligand_instance()}:
            raise ValueError('Instance launch response differs from the dispatch receipt')
        return response
    record = check_record(hpc.remote(['inspect', run_id]))
    if record['run_id'] != run_id or record['state'] != 'QUEUED' or record['receipt'] is not None:
        raise ValueError('VS dispatch requires the exact unreserved queued task')
    argv = hpc.create_args(record, image)
    hpc.call(argv + ['--dry-run'])
    record['scheduler_image'] = image
    with path.open('x') as stream:
        path.chmod(0o600)
        json.dump(record, stream)
    # An uncertain response after this point is only observed by its saved name.
    hpc.remote(['reserve', run_id, '--digest', record['spec_sha256']])
    response = hpc.call(argv)
    if response.get('name') != record['name']:
        raise ValueError('VS creation response differs from reserved identity; inspect, never replay')
    return {'name':record['name'], 'submitted':True, 'scientific_acceptance':False}


def status(run_id, path, *, check_record=check):
    saved = json.loads(path.read_text())
    if saved.get('execution_instance') in {'asgbie3', 'asgbie4'}:
        if saved['execution_instance'] != hpc.ligand_instance():
            raise ValueError('Instance receipt differs from the installation executor')
        if saved['run_id'] != run_id or saved['release_binding'] != hpc.RELEASE_BINDING:
            raise ValueError('Instance receipt differs from the registered task or release')
        observed = hpc.remote(['inspect-instance', run_id])
        for field in ('run_id', 'spec_sha256', 'execution_instance'):
            if saved[field] != observed[field]:
                raise ValueError('Instance observation differs from the dispatch receipt')
        bootstrap = observed.get('bootstrap', {})
        if (os.environ.get('COVALENT_INSTANCE_RECOVER_RUN') == run_id
                and bootstrap.get('recovery_eligible') is True
                and observed['state'] == 'QUEUED' and not observed['worker_claimed']):
            # Explicit installation-owned recovery for this exact original run.
            # Remote proof and an exclusive marker prevent replay or concurrency.
            return hpc.remote(['recover-instance', run_id, '--digest', saved['spec_sha256']])
        failed = bootstrap.get('state') in {'LAUNCH_FAILED', 'LAUNCH_UNCERTAIN'}
        return {'name': hpc.ligand_instance(), 'scheduler_state': bootstrap['state'] if failed else observed['state'],
                'platform_state': observed['state'], 'scientific_acceptance': False,
                'md_ready': False, 'bootstrap': bootstrap,
                **({'error': bootstrap['error']} if failed else {})}
    record = check_record(saved)
    if record['run_id'] != run_id:
        raise ValueError('VS receipt belongs to another registered task')
    observed = check_record(hpc.remote(['inspect', run_id]))
    hpc.verify_record(record, observed)
    scheduler = hpc.call(['hpc', 'status', record['name'], '--workspace', 'CPU资源空间'])
    if scheduler.get('name') != record['name']:
        raise ValueError('VS scheduler observation changed identity')
    state = scheduler['status'].upper()
    failure = {'FAILED':'FAILED', 'FAILED_RETAINING':'FAILED', 'CANCELLED':'CANCELLED',
               'STOPPED':'CANCELLED', 'TIMED_OUT':'TIMED_OUT'}.get(state)
    if failure:
        hpc.remote(['fail', run_id, '--digest', record['spec_sha256'], '--name', record['name'], '--state', failure])
    job = check_record(hpc.remote(['inspect', run_id]))
    hpc.verify_record(record, job)
    return {'name':record['name'], 'scheduler_state':state, 'platform_state':job['state'],
            'scientific_acceptance':False, 'md_ready':False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['submit','status'])
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--binding', type=Path, required=True)
    parser.add_argument('--image', default='slurm-gromacs:0.1.1-alpha.1')
    args = parser.parse_args()
    if args.state.is_symlink():
        raise ValueError('VS dispatch receipt cannot be a symbolic link')
    saved = json.loads(args.state.read_text()) if args.action == 'status' else None
    hpc.configure_root(args.root, saved, required=True)
    selected = json.loads(args.binding.read_text())
    # Validate the installed management binding, then observe a saved task with
    # its original immutable worker release. Never migrate an existing spec.
    hpc.configure_release_binding(selected)
    if saved:
        hpc.configure_release_binding(saved.get('release_binding'), saved)
    value = submit(args.run_id, args.state, args.image) if args.action == 'submit' else status(args.run_id, args.state)
    print(json.dumps(value))


if __name__ == '__main__':
    main()
