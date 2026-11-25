"""One-shot GPU equilibration dispatch; uncertain submissions are never replayed."""
import argparse
import json
from pathlib import Path
import shlex
import dispatch_ligand_hpc as hpc


def command(args):
    tokens = shlex.split(hpc.command(args, {'cpu_count': 4}))
    tokens[tokens.index(hpc.ROOT + '/scripts/ligand_external_hpc.py')] = hpc.ROOT + '/scripts/ligand_external_openmm.py'
    return shlex.join(tokens)


def remote(args):
    response = hpc.call(['notebook', 'exec', 'asgbie3', '--workspace', 'CPU资源空间',
                         '--timeout', '60', command(args)])
    if response['returncode']:
        raise RuntimeError('OpenMM dispatch evidence rejected; never replay create')
    return json.loads(response['output'])


def create_args(record):
    from covscreen.external_openmm import configured_profile, validate_budget
    from covscreen.jobs import ResourceBudget
    hpc.bind_record(record)
    profile = configured_profile({'scheduler_profile': record['scheduler_profile']})
    budget = ResourceBudget.model_validate(record['budget'])
    validate_budget(budget)
    if record['minutes'] != budget.max_runtime_minutes or record['engine_cpu_threads'] != 4:
        raise ValueError('Dispatcher runtime or thread count differs from the approved plan')
    args = ['job', 'create', '--name', record['name']]
    for key in ('workspace', 'project', 'group', 'quota', 'image'):
        args.extend(['--' + key, profile[key]])
    return args + ['--nodes', '1', '--shm-size', '16', '--no-auto-fault-tolerance',
        '--no-enable-notification', '--max-time', str(record['minutes'] / 60),
        '--command', command(['run', record['run_id'], '--digest', record['spec_sha256']])]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preflight', 'submit', 'status'])
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--root')
    parser.add_argument('--binding', type=Path)
    args = parser.parse_args()
    saved = json.loads(args.state.read_text()) if args.action == 'status' else None
    hpc.configure_root(args.root, saved, required=args.action != 'status')
    hpc.configure_release_binding(json.loads(args.binding.read_text()) if args.binding else None, saved)
    if args.action in {'preflight', 'submit'}:
        record = hpc.bind_record(remote(['inspect', args.run_id]))
        if record['receipt'] or record['state'] != 'QUEUED' or args.state.exists():
            raise ValueError('Only an unreserved queued job can be submitted; use status')
        create = create_args(record)
        hpc.call(create + ['--dry-run'])
        if args.action == 'preflight':
            print(json.dumps({'preflight': 'PASS', 'compute_submitted': False}))
            return
        args.state.parent.mkdir(parents=True, exist_ok=True)
        with args.state.open('x') as stream:
            json.dump(record, stream)
        remote(['reserve', args.run_id, '--digest', record['spec_sha256']])
        response = hpc.call(create)
        if response.get('name') != record['name']:
            raise RuntimeError('Submission identity uncertain; inspect exact name, never retry')
        print(json.dumps({'name': record['name'], 'submitted': True}))
        return
    if saved['run_id'] != args.run_id:
        raise ValueError('Dispatch receipt belongs to another run')
    hpc.verify_record(saved, remote(['inspect', args.run_id]))
    observed = hpc.call(['job', 'status', saved['name'], '--workspace', saved['scheduler_profile']['workspace']])
    if observed['name'] != saved['name']:
        raise ValueError('Scheduler identity mismatch')
    state = observed['status'].upper()
    failure = {'FAILED': 'FAILED', 'FAILED_RETAINING': 'FAILED', 'CANCELLED': 'CANCELLED',
        'STOPPED': 'CANCELLED', 'JOB_STOPPED': 'CANCELLED', 'TIMED_OUT': 'TIMED_OUT'}.get(state)
    if failure:
        remote(['fail', args.run_id, '--digest', saved['spec_sha256'], '--name', saved['name'], '--state', failure])
    platform = remote(['inspect', args.run_id])
    print(json.dumps({'name': saved['name'], 'scheduler_state': state, 'platform_state': platform['state']}))


if __name__ == '__main__':
    main()
