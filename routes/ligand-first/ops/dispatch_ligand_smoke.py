"""Explicit one-shot smoke dispatch; no watcher, discovery submission or retry."""
import argparse
import json
from pathlib import Path
import shlex
import re
import os

import dispatch_ligand_hpc as hpc


def command(args, budget=None):
    tokens = shlex.split(hpc.command(args, budget))
    tokens[tokens.index(hpc.ROOT + '/scripts/ligand_external_hpc.py')] = hpc.ROOT + '/scripts/ligand_external_inspire.py'
    return shlex.join(tokens)


def remote(args):
    instance = hpc.ligand_instance()
    if os.environ.get('COVALENT_LIGAND_TRANSPORT') == 'jupyter':
        from inspire import InspireClient
        with InspireClient(account=hpc.CLI[hpc.CLI.index('--account') + 1], allow_browser=True) as client:
            result = client.notebooks.exec(instance, workspace='CPU资源空间',
                command=command(args), transport='jupyter', timeout=60)
        if not result.completed or result.truncated:
            raise RuntimeError('Incomplete preparation response; do not replay')
        response = {'returncode': result.returncode, 'output': result.output}
    else:
        response = hpc.call(['notebook', 'exec', instance, '--workspace', 'CPU资源空间',
            '--timeout', '60', command(args)])
    if response['returncode']:
        raise RuntimeError('Remote validation rejected; inspect evidence, never replay create')
    return json.loads(response['output'])


def create_args(record, excluded_nodes=()):
    from covscreen.docking_policy import require_engine_enabled
    require_engine_enabled(record["engine"])
    hpc.bind_record(record)
    if any(not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,252}', node) for node in excluded_nodes):
        raise ValueError('Invalid excluded node name')
    budget = record['budget']
    entry = command(['run', record['run_id'], '--digest', record['spec_sha256']], budget)
    if record['workload'] == 'job':
        if (budget['gpu_count'], budget['cpu_count'], budget['memory_gb']) != (1, 20, 200):
            raise ValueError('GPU quota differs from approved smoke budget')
        args = ['job', 'create', '--name', record['name'], '--workspace', '分布式训练空间',
            '--project', '原子级化学反应基座模型2.0', '--group', '开发区-H200-3号机房-2-cuda12.8版本',
            '--quota', '1,20,200', '--image', 'pytorch-inspire-base:2.4.0-cuda12.4-cudnn9-runtime',
            '--nodes', '1', '--shm-size', '16', '--no-auto-fault-tolerance',
            '--no-enable-notification', '--max-time', str(record['minutes'] / 60), '--command', entry]
        for node in dict.fromkeys(excluded_nodes):
            args.extend(['--exclude-node', node])
        return args
    if excluded_nodes:
        raise ValueError('Node exclusion is only supported for GPU jobs')
    if (record['workload'] != 'hpc'
            or budget['gpu_count'] != 0
            or budget['cpu_count'] not in {2, 4}
            or budget['memory_gb'] != 16):
        raise ValueError('CPU quota differs from approved smoke budget')
    args = hpc.create_args(record, 'slurm-gromacs:0.1.1-alpha.1')
    args[args.index('--entrypoint') + 1] = 'srun ' + entry
    return args


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preflight', 'submit', 'replace-scheduler', 'status'])
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--root', help='Explicit execution root; old status receipts retain their historical root')
    parser.add_argument('--binding', type=Path, help='Exact isolated release/data binding; frozen in receipt')
    parser.add_argument('--exclude-node', action='append', default=[],
                        help='Exclude a live-identified faulty GPU node; does not change resource budget')
    args = parser.parse_args()
    saved = json.loads(args.state.read_text()) if args.action == 'status' else None
    hpc.configure_root(args.root, saved, required=args.action != 'status')
    hpc.configure_release_binding(json.loads(args.binding.read_text()) if args.binding else None, saved)
    if args.action in {'preflight', 'submit'}:
        record = hpc.bind_record(remote(['inspect', args.run_id]))
        if record['receipt'] or record['state'] != 'QUEUED' or args.state.exists():
            raise ValueError('Not an unreserved queued job; use status')
        create = create_args(record, args.exclude_node)
        hpc.call(create + ['--dry-run'])  # Live quota/project/image resolution before reserve.
        if args.action == 'preflight':
            print(json.dumps({'run_id': args.run_id, 'preflight': 'PASS', 'compute_submitted': False}))
            return
        args.state.parent.mkdir(parents=True, exist_ok=True)
        record['excluded_nodes'] = list(dict.fromkeys(args.exclude_node))
        with args.state.open('x') as stream:
            json.dump(record, stream)
        remote(['reserve', args.run_id, '--digest', record['spec_sha256']])
        response = hpc.call(create)
        if response.get('name') != record['name']:
            raise RuntimeError('Submission identity uncertain; reconcile, never retry')
        print(json.dumps({'name': record['name'], 'submitted': True}))
        return
    if args.action == 'replace-scheduler':
        if args.state.exists():
            raise ValueError('Replacement record exists; use status, never replay replacement')
        record = hpc.bind_record(remote(['inspect', args.run_id]))
        if (not record.get('scheduler_replacement_eligible')
                or record.get('scheduler_replacement') is not None
                or record['state'] != 'FAILED'):
            raise ValueError('Run is not eligible for a pre-worker scheduler replacement')
        replacement = dict(record)
        replacement['name'] = record['scheduler_replacement_name']
        create = create_args(replacement, args.exclude_node)
        hpc.call(create + ['--dry-run'])
        args.state.parent.mkdir(parents=True, exist_ok=True)
        with args.state.open('x') as stream:
            json.dump(replacement, stream)
        observed = remote(['replace-scheduler', args.run_id, '--digest', record['spec_sha256'],
            '--name', record['original_name'], '--state', 'FAILED'])
        if observed['name'] != replacement['name'] or observed['state'] != 'QUEUED':
            raise RuntimeError('Replacement reservation identity mismatch; inspect, never replay')
        response = hpc.call(create)
        if response.get('name') != replacement['name']:
            raise RuntimeError('Replacement submission identity uncertain; reconcile, never replay')
        print(json.dumps({'name': replacement['name'], 'scheduler_replacement': True,
                          'scientific_attempt_unchanged': True, 'submitted': True}))
        return
    record = json.loads(args.state.read_text())
    if record['run_id'] != args.run_id:
        raise ValueError('State belongs to another job')
    hpc.verify_record(record, remote(['inspect', args.run_id]))
    workspace = '分布式训练空间' if record['workload'] == 'job' else 'CPU资源空间'
    observed = hpc.call([record['workload'], 'status', record['name'], '--workspace', workspace])
    if observed['name'] != record['name']:
        raise ValueError('Scheduler identity mismatch')
    state = observed['status'].upper()
    failure = {'FAILED': 'FAILED', 'FAILED_RETAINING': 'FAILED', 'CANCELLED': 'CANCELLED',
        'STOPPED': 'CANCELLED', 'JOB_STOPPED': 'CANCELLED', 'TIMED_OUT': 'TIMED_OUT'}.get(state)
    if failure:
        remote(['fail', args.run_id, '--digest', record['spec_sha256'], '--name', record['name'], '--state', failure])
    platform = remote(['inspect', args.run_id])
    print(json.dumps({'name': record['name'], 'scheduler_state': state, 'platform_state': platform['state']}))


if __name__ == '__main__':
    main()
