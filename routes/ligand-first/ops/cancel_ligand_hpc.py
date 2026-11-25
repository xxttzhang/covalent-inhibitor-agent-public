"""Stop only an existing HPC reservation with a remotely verified signed intent.

No create/retry/fail calls: scheduler termination alone is not proof that a
vendor job launched outside its container was stopped. Keep those facts separate.
"""
import json
import argparse
import os
from pathlib import Path
import shlex

import dispatch_ligand_hpc as hpc

TERMINAL = frozenset({'FAILED', 'CANCELLED', 'STOPPED', 'TIMED_OUT', 'SUCCEEDED'})
ACTIVE = frozenset({'QUEUED', 'QUEUEING', 'PENDING', 'CREATING', 'RUNNING'})

# The management release is independently pinned. It may inspect signed older
# jobs, but has no worker/run entry point and never edits their source binding.
MANAGEMENT_LAUNCH = r'''
import hashlib,json,pathlib,sys
binding=json.loads(sys.argv[1]); run=sys.argv[2]
root=pathlib.Path(binding['release_root']); data=pathlib.Path(binding['data_root'])
if root.is_symlink() or root.resolve()!=root or root.name!=binding['release_id']:
    raise ValueError('Invalid management release root')
manifest=root/'manifest.json'
if manifest.is_symlink(): raise ValueError('Symlinked management manifest')
raw=manifest.read_bytes(); value=json.loads(raw)
canonical=json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
if (hashlib.sha256(raw).hexdigest()!=binding['manifest_sha256']
        or hashlib.sha256(canonical).hexdigest()!=root.name or raw!=canonical+b'\n'
        or value.get('version')!='screening-code-release-v1' or not value.get('files')):
    raise ValueError('Management release manifest mismatch')
for name,digest in value['files'].items():
    path=root/name
    if (path.is_symlink() or not path.resolve().is_relative_to(root)
            or hashlib.sha256(path.read_bytes()).hexdigest()!=digest):
        raise ValueError('Management source changed')
sys.path.insert(0,str(root/'science/src'))
from covscreen.jobs import ArtifactRegistry
from covscreen.scheduler_cancellation import read_intent
from covscreen.external_hpc import inspect
registry=ArtifactRegistry(data)
record=inspect(data,run); intent=read_intent(registry,run)
if len(sys.argv)>3:
    from covscreen.scheduler_cancellation import confirm_unstarted
    print(json.dumps(confirm_unstarted(registry,run,json.loads(sys.argv[3]))))
    raise SystemExit(0)
record['cancellation']={'scope':intent['scope'],'requested_at':intent['requested_at'],
                        'termination_confirmed':False}
print(json.dumps(record))
'''


def management_remote(args: list[str]) -> dict:
    if (not hpc.RELEASE_BINDING or not ((len(args) == 2 and args[0] == 'cancel-inspect')
            or (len(args) == 3 and args[0] == 'cancel-confirm'))):
        raise ValueError('Only bound cancellation inspection is supported')
    command = shlex.join(['env', '-u', 'PYTHONPATH', '-u', 'PYTHONHOME',
        '-u', 'LD_PRELOAD', '-u', 'LD_AUDIT', '-u', 'LD_LIBRARY_PATH',
        'COVALENT_APPROVAL_KEY_FILE=' + os.environ.get('COVALENT_REMOTE_APPROVAL_KEY_FILE',
                                                       hpc.ROOT + '/.secrets/native-approval.key'),
        str(Path(hpc.ROOT).parent / 'environment/host/bin/python'), '-B', '-c', MANAGEMENT_LAUNCH,
        json.dumps(hpc.RELEASE_BINDING, separators=(',', ':')), args[1], *args[2:]])
    response = hpc.call(['notebook', 'exec', 'asgbie3', '--workspace', 'CPU资源空间',
                         '--timeout', '60', command])
    if response.get('returncode') != 0:
        raise ValueError('Cancellation management inspection failed; no stop sent')
    return json.loads(response['output'])


def stop(run_id: str, path: Path, *, observe_only: bool = False) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError('A regular existing dispatch receipt is required')
    saved = json.loads(path.read_text())
    if saved.get('dispatch_root') != hpc.ROOT:
        raise ValueError('Cancellation data root differs from original dispatch')
    if saved.get('run_id') != run_id or saved.get('route') != 'ligand-first':
        raise ValueError('Cancellation receipt belongs to another run or route')
    observed = management_remote(['cancel-inspect', run_id])
    hpc.verify_record(saved, observed)
    expected = {'kind': 'cancel', 'run_id': run_id, 'spec_sha256': saved['spec_sha256'],
                'scheduler': {'backend': 'external-hpc', 'name': saved['name']}}
    if observed.get('cancellation', {}).get('scope') != expected:
        raise ValueError('Verified native cancellation scope differs from dispatch receipt')
    args = ['hpc', 'status', saved['name'], '--workspace', 'CPU资源空间']
    before = hpc.call(args)
    if before.get('name') != saved['name']:
        raise ValueError('Scheduler cancellation observation changed identity')
    state = str(before.get('status', '')).upper()
    if state not in TERMINAL | ACTIVE | {'STOPPING', 'CANCELLING'}:
        raise ValueError('Unknown scheduler state; inspect without sending stop')
    marker = path.with_name(path.name + '.stop-request.json')
    if marker.is_symlink():
        raise ValueError('Symlinked scheduler stop receipt')
    if marker.exists():
        if json.loads(marker.read_text()).get('scope') != expected:
            raise ValueError('Existing stop receipt has a different identity')
    elif state in ACTIVE and not observe_only:
        # Persist before the call: an ambiguous response must never replay stop.
        with marker.open('x') as stream:
            marker.chmod(0o600)
            json.dump({'scope': expected, 'state': 'stop_attempt_reserved'}, stream)
        hpc.call(['hpc', 'stop', saved['name'], '--workspace', 'CPU资源空间'])
    after = hpc.call(args)
    if after.get('name') != saved['name']:
        raise ValueError('Scheduler stop observation changed identity')
    state = str(after.get('status', '')).upper()
    confirmation = (management_remote(['cancel-confirm', run_id, json.dumps({
        'run_id': run_id, 'spec_sha256': saved['spec_sha256'], 'name': saved['name'], 'status': state})])
        if state in {'STOPPED', 'CANCELLED'} else {'cancellation_confirmed': False})
    return {'run_id': run_id, 'name': saved['name'], 'scheduler_state': state,
            'scheduler_terminal_observed': state in TERMINAL,
            'cancellation_confirmed': confirmation.get('cancellation_confirmed') is True,
            'termination_evidence': confirmation, 'vendor_termination_verified': False,
            'observation_only': observe_only,
            'next_action': 'Retain original run; verify worker and vendor termination before confirming cancellation.'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--state', required=True, type=Path)
    parser.add_argument('--root', required=True)
    parser.add_argument('--binding', required=True, type=Path)
    parser.add_argument('--observe-only', action='store_true')
    args = parser.parse_args()
    hpc.configure_root(args.root, required=True)
    hpc.configure_release_binding(json.loads(args.binding.read_text()))
    print(json.dumps(stop(args.run_id, args.state, observe_only=args.observe_only)))


if __name__ == '__main__':
    main()
