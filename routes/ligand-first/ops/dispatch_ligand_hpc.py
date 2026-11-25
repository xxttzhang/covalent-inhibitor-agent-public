"""Dispatch/observe one approved platform job through Inspire, never replay create."""
import argparse
import json
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import time
import sys
import math
import os
import re
import stat

BASE = os.environ.get('COVSCREEN_LEGACY_BASE', '')
ROOT = os.environ.get('COVSCREEN_LEGACY_ROOT', '')
CLI = [os.environ.get('INSPIRE_CLI', 'inspire'), '--json', '--no-env-file', '--account', 'default']
LEGACY_ROOT = ROOT
RELEASE_BINDING = None


def configure_release_binding(selected=None, record=None):
    """Freeze an explicit isolated consumer binding in the dispatch receipt."""
    global RELEASE_BINDING
    saved = (record or {}).get('release_binding')
    if saved and selected and saved != selected:
        raise ValueError('Release binding differs from frozen receipt')
    binding = saved or selected
    if binding is not None:
        import re
        if set(binding) != {'release_id', 'manifest_sha256', 'release_root', 'data_root'}:
            raise ValueError('Expected exact release/data binding')
        for key in ('release_id', 'manifest_sha256'):
            if not isinstance(binding[key], str) or not re.fullmatch(r'[a-f0-9]{64}', binding[key]):
                raise ValueError('Invalid release digest')
        for key in ('release_root', 'data_root'):
            value = binding[key]
            if (not isinstance(value, str) or not value.startswith('/inspire/')
                    or str(PurePosixPath(value)) != value or '..' in PurePosixPath(value).parts
                    or any(ord(char) < 32 for char in value)):
                raise ValueError('Invalid binding path')
        release, data = (PurePosixPath(binding[key]) for key in ('release_root', 'data_root'))
        if (str(data) != ROOT or release.name != binding['release_id']
                or release == data or release in data.parents or data in release.parents):
            raise ValueError('Release/data binding mismatch')
    RELEASE_BINDING = dict(binding) if binding else None
    return RELEASE_BINDING


def configure_root(selected=None, record=None, required=False):
    """Bind one invocation; old receipts retain their historical root."""
    global ROOT
    saved = (record or {}).get('dispatch_root')
    if saved and selected and selected != saved:
        raise ValueError('Dispatch root differs from frozen receipt')
    if required and not selected:
        raise ValueError('New dispatch requires an explicit --root; no historical default')
    value = saved or selected or LEGACY_ROOT
    if (not isinstance(value, str) or not value.startswith('/inspire/')
            or str(PurePosixPath(value)) != value or '..' in PurePosixPath(value).parts
            or any(ord(char) < 32 for char in value)):
        raise ValueError('Dispatch root must be a normalized absolute Inspire project path')
    ROOT = value
    return value


def bind_record(record):
    if record.get('dispatch_root', ROOT) != ROOT:
        raise ValueError('Dispatch root differs from frozen receipt')
    if record.get('release_binding') and record['release_binding'] != RELEASE_BINDING:
        raise ValueError('Release binding differs from frozen receipt')
    return {**record, 'dispatch_root': ROOT,
            **({'release_binding': RELEASE_BINDING} if RELEASE_BINDING else {})}


def verify_record(saved, observed):
    """Reject another run/spec before synchronizing scheduler failure state."""
    for key in ('run_id', 'spec_sha256', 'name'):
        if not saved.get(key) or saved.get(key) != observed.get(key):
            raise ValueError('Remote run identity differs from frozen receipt: ' + key)


def call(args, *, timeout=110):
    result = subprocess.run(CLI + args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Keep the CLI's structured error: losing it makes a rejected create
        # indistinguishable from an uncertain response after successful creation.
        try:
            error = json.loads(result.stdout.strip() or result.stderr.strip()).get('error', {})
        except json.JSONDecodeError:
            error = {'type': 'UnstructuredCLIError', 'message': result.stderr.strip()[:4000]}
        raise RuntimeError(json.dumps({'returncode': result.returncode, 'error': error,
            'action': 'Inspect exact workload; never automatically resubmit'}, ensure_ascii=False))
    response = json.loads(result.stdout)
    if response.get('success') is not True:
        raise RuntimeError('Inspire response rejected')
    return response['data']


def runtime_profile():
    """Use the same installation-owned runtime profile as the science connector."""
    filename = os.environ.get('COVALENT_SCIENCE_RUNTIME_PROFILE')
    if not filename:
        return None
    path = Path(filename)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('Scientific runtime profile must be an owner-only regular file')
    value = json.loads(path.read_text())
    if set(value) not in ({'python', 'sha256', 'lock', 'lock_sha256'},
                           {'python', 'sha256', 'lock', 'lock_sha256', 'covalentizer'}):
        raise ValueError('Expected an exact scientific runtime profile')
    for key in ('python', 'lock'):
        item = value[key]
        if (not isinstance(item, str) or not item.startswith('/inspire/')
                or str(PurePosixPath(item)) != item or '..' in PurePosixPath(item).parts
                or any(ord(char) < 32 for char in item)):
            raise ValueError('Invalid scientific runtime path')
    for key in ('sha256', 'lock_sha256'):
        if not isinstance(value[key], str) or not re.fullmatch(r'[a-f0-9]{64}', value[key]):
            raise ValueError('Invalid scientific runtime digest')
    return {key: value[key] for key in ('python', 'sha256', 'lock', 'lock_sha256')}


def ligand_instance():
    value = os.environ.get('COVALENT_LIGAND_VS_INSTANCE', '') or 'asgbie3'
    if value not in {'asgbie3', 'asgbie4'}:
        raise ValueError('Unregistered Ligand CPU execution instance')
    return value


def command(args, budget=None, *, instance_name=None):
    instance_name = instance_name or ligand_instance()
    if instance_name not in {'asgbie2', 'asgbie3', 'asgbie4'}:
        raise ValueError('Unregistered CPU execution instance')
    budget = budget or {'cpu_count': 4}
    cpus = str(budget['cpu_count'])
    instance = args[:1] in (['inspect-instance'], ['launch-instance'], ['run-instance'], ['recover-instance'])
    # The Linux subreaper must remain single-threaded. Vendor workers receive
    # their CPU budget separately through the signed recipe and CPU affinity.
    threads = '1' if instance else cpus
    runtime = runtime_profile()
    if instance and runtime is None:
        raise ValueError('Instance execution requires the scientific host runtime profile')
    if runtime is None and not BASE.startswith('/inspire/'):
        raise ValueError('Legacy execution requires explicit COVSCREEN_LEGACY_BASE')
    prefix = ['env', '-u', 'LD_PRELOAD', '-u', 'LD_AUDIT', '-u', 'PYTHONHOME',
              '-u', 'LD_LIBRARY_PATH', '-u', 'PYTHONPATH',
              *([] if runtime else ['PYTHONPATH=' + ROOT + '/src:' + BASE + '/carsidock_gpu_env/lib/python3.11/site-packages']),
              'COVALENT_APPROVAL_KEY_FILE=' + os.environ.get('COVALENT_REMOTE_APPROVAL_KEY_FILE', ROOT + '/.secrets/native-approval.key'),
              'OMP_NUM_THREADS=' + threads, 'OPENBLAS_NUM_THREADS=' + threads, 'MKL_NUM_THREADS=' + threads,
              'NUMEXPR_NUM_THREADS=' + threads,
              'PYTHONDONTWRITEBYTECODE=1', 'PYTHONNOUSERSITE=1',
              'COVALENT_DISPATCH_RUNTIME=' + json.dumps(runtime, separators=(',', ':')),
              'COVALENT_INSTANCE_RECOVER_TERMINAL=' + os.environ.get('COVALENT_INSTANCE_RECOVER_TERMINAL', ''),
              'COVALENT_DISPATCH_BINDING=' + json.dumps(RELEASE_BINDING, separators=(',', ':')),
              'COVALENT_DISPATCH_INSTANCE=' + instance_name,
              'COVSCREEN_LEGACY_RELEASES=' + os.environ.get('COVSCREEN_LEGACY_RELEASES', ''),
              *(['COVALENT_LIGAND_VS_INSTANCE=asgbie4'] if instance_name == 'asgbie4' else []),
              runtime['python'] if runtime else BASE + '/python311_runtime/bin/python', '-B', '-c', RELEASE_LAUNCH,
              ROOT + '/scripts/ligand_external_hpc.py']
    return shlex.join(prefix + args)


# Select by the persisted job, never by a moving "current release" pointer.
# Legacy receipts still invoke their original script. New jobs carry a signed
# source_release and are checked again by the selected external worker.
RELEASE_LAUNCH = r'''
import hashlib,json,os,pathlib,re,runpy,sys
entry=pathlib.Path(sys.argv[1]); args=sys.argv[2:]; project=entry.parent.parent
binding=json.loads(os.environ.get('COVALENT_DISPATCH_BINDING','null'))
instance=os.environ.get('COVALENT_DISPATCH_INSTANCE','asgbie3')
if instance not in {'asgbie2','asgbie3','asgbie4'}: raise ValueError('Unregistered CPU execution instance')
runtime=json.loads(os.environ.get('COVALENT_DISPATCH_RUNTIME','null'))
if runtime:
    for filename,checksum in ((runtime['python'],runtime['sha256']), (runtime['lock'],runtime['lock_sha256'])):
        if hashlib.sha256(pathlib.Path(filename).read_bytes()).hexdigest()!=checksum:
            raise ValueError('Scientific host runtime asset changed')
    if pathlib.Path(sys.executable).resolve()!=pathlib.Path(runtime['python']).resolve():
        raise ValueError('Worker Python differs from scientific host runtime')
if args and args[0] in {'launch-instance','run-instance','inspect-instance','recover-instance'} and (not binding or len(args)<2 or not re.fullmatch(r'[A-Za-z0-9_-]+',args[1])):
    raise ValueError('Instance execution requires a fixed release binding and registered run ID')
if binding and str(project)!=binding['data_root']:
    raise ValueError('Dispatcher data root differs from explicit binding')
if len(args)>1 and re.fullmatch(r'[A-Za-z0-9_-]+',args[1]):
    specfile=project/'state/jobs'/args[1]/'spec.json'
    spec=json.loads(specfile.read_text()) if specfile.is_file() else {}
    release=spec.get('provenance',{}).get('source_release')
    if binding and (not release or any(release.get(key)!=binding[field] for key,field in
            [('root','release_root'),('release_id','release_id'),('manifest_sha256','manifest_sha256')])):
        raise ValueError('Job source differs from explicit release binding')
    if release:
        root=pathlib.Path(release['root'])
        allowed=os.environ.get('COVSCREEN_LEGACY_RELEASES')
        if (not binding and (not allowed or root.parent!=pathlib.Path(allowed))) or root.is_symlink() or root.name!=release['release_id'] or not re.fullmatch(r'[a-f0-9]{64}',root.name):
            raise ValueError('Job source release is outside registered releases')
        if (root/'manifest.json').is_symlink(): raise ValueError('Manifest cannot be a symbolic link')
        raw=(root/'manifest.json').read_bytes(); manifest=json.loads(raw)
        encoded=json.dumps(manifest,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
        if hashlib.sha256(raw).hexdigest()!=release['manifest_sha256'] or hashlib.sha256(encoded).hexdigest()!=root.name:
            raise ValueError('Job source release manifest changed')
        for name,digest in manifest['files'].items():
            path=root/name
            if path.is_symlink() or not path.resolve().is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                raise ValueError('Job source file changed')
        sys.path.insert(0,str(root/'science/src'))
        from covscreen.source_release import frozen_release
        if frozen_release()!=release: raise ValueError('Job release differs from installed source')
        entry=root/'routes/ligand-first/scripts'/entry.name
        os.environ['COVSCREEN_PROJECT_ROOT']=str(project)
sys.path.insert(0,str(entry.parent))
if args[:1] in (['inspect-instance'], ['launch-instance'], ['recover-instance']):
    # Exercise the same imports and Linux supervisor setup used by execute()
    # before publishing a launch request. This does not claim or run a job.
    from covscreen.instance_claim import inspect
    from covscreen.execution import _execute_claimed_job
    from covscreen.instance_process import InstanceProcessTree
    InstanceProcessTree()
    record=inspect(project,args[1],instance)
    job=project/'state/jobs'/args[1]
    record['bootstrap']=launch_observation(job,args[1])
    if args[:1]==['inspect-instance']:
        print(json.dumps(record))
        raise SystemExit(0)
if args[:1] in (['launch-instance'], ['recover-instance']):
    import subprocess
    # Reuse the preflight record above; the worker revalidates before claiming.
    if args[2:]!=['--digest',record['spec_sha256']] or record['state']!='QUEUED' or record['worker_claimed']:
        raise ValueError('Instance launch requires the exact unclaimed queued task')
    job=project/'state/jobs'/args[1]
    logfile=reserve_recovery(job,args[1],record['spec_sha256']) if args[0]=='recover-instance' else job/'instance-launch.log'
    with logfile.open('xb') as log:
        subprocess.Popen([sys.executable,'-B','-c',sys.orig_argv[sys.orig_argv.index('-c')+1],sys.argv[1],'run-instance',*args[1:]],
            stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    print(json.dumps({'submitted':True,'name':instance}))
    raise SystemExit(0)
sys.argv=[str(entry),*args]
try:
    if args[:1]==['run-instance']:
        # Do not traverse the unrelated HPC entrypoint: it imports retained-pose
        # pandas/Arrow helpers, whose allocator starts a background thread.
        # Use exactly the dedicated worker imports exercised by preflight.
        from covscreen.instance_claim import execute
        if len(args)!=4 or args[2]!='--digest':
            raise ValueError('Instance worker requires the exact registered digest')
        result=execute(project,args[1],args[3],instance)
        print(json.dumps({'run_id':result.run_id,'state':result.state}))
        raise SystemExit(0 if result.state=='COMPLETED' else 1)
    runpy.run_path(str(entry),run_name='__main__')
except Exception as error:
    if args[:1]==['run-instance']:
        job=project/'state/jobs'/args[1]
        if not (job/'instance_worker_claim').exists():
            with (job/'instance-bootstrap-terminal.json').open('x') as stream:
                json.dump({'run_id':args[1],'error':type(error).__name__+': '+str(error)},stream)
    raise
'''

# This helper is shipped as part of the fixed local dispatcher release. Its
# source is embedded in the command; the remote scientific release stays fixed.
RELEASE_LAUNCH = Path(__file__).resolve().with_name('instance_launch.py').read_text() + '\n' + RELEASE_LAUNCH


def remote(args, *, instance=None, transport=None):
    instance = instance or ligand_instance()
    transport = transport or os.environ.get('COVALENT_LIGAND_TRANSPORT', 'cli')
    # Inspire discards remote stderr on nonzero exit. Keep the child exit status
    # inside a JSON envelope so failures remain visible without implying success.
    program = ('import json,subprocess; '
               'p=subprocess.run(' + repr(shlex.split(command(args, instance_name=instance))) + ',capture_output=True,text=True); '
               'print(json.dumps({"returncode":p.returncode,"output":p.stdout,"stderr":p.stderr}))')
    seconds = 300 if args[0] in {'inspect-instance', 'launch-instance', 'recover-instance'} else 60
    if transport == 'jupyter':
        from inspire import InspireClient
        with InspireClient(account=CLI[CLI.index('--account') + 1], allow_browser=True) as client:
            result = client.notebooks.exec(instance, workspace='CPU资源空间',
                command=shlex.join(['python3', '-B', '-c', program]), transport='jupyter', timeout=seconds)
        if not result.completed or result.truncated:
            raise RuntimeError('Incomplete instance response; observe original task without replay')
        response = {'returncode': result.returncode, 'output': result.output}
    elif transport == 'cli':
        response = call(['notebook', 'exec', instance, '--workspace', 'CPU资源空间',
                         '--timeout', str(seconds), shlex.join(['python3', '-B', '-c', program])],
                        timeout=seconds + 50)
    else:
        raise ValueError('Unsupported instance transport')
    if response['returncode']:
        raise RuntimeError('Remote evidence/action rejected; no automatic retry')
    child = json.loads(response['output'])
    if child['returncode']:
        raise RuntimeError(json.dumps({'returncode': child['returncode'],
            'error': child['stderr'][-8000:], 'action': args[0],
            'instruction': 'Inspect original task; never automatically resubmit'}, ensure_ascii=False))
    return json.loads(child['output'])


def create_args(record, image):
    bind_record(record)
    budget = record['budget']
    cpus = budget['cpu_count']
    memory = budget['memory_gb']
    if isinstance(cpus, bool) or not isinstance(cpus, int) or cpus < 1:
        raise ValueError('HPC requires a positive integral CPU count; request a new compatible budget')
    if not math.isfinite(memory) or memory != int(memory) or memory < cpus:
        raise ValueError('HPC requires integral node GiB and at least 1 GiB per CPU; request a new compatible budget')
    memory = int(memory)
    # CLI requires integer GiB. Never round allocated node memory above approval.
    # Slurm bookkeeping rounds down; cgroup memory remains the signed node quota.
    memory_per_cpu = memory // cpus
    entry = 'srun ' + command(['run', record['run_id'], '--digest', record['spec_sha256']], budget)
    # Never enlarge the allocated node beyond the approved resource budget.
    # The CLI preflight must reject an unavailable SKU before reservation;
    # selecting a larger node requires a new explicit resource approval.
    return ['hpc', 'create', '--name', record['name'], '--workspace', 'CPU资源空间',
            '--project', '原子级化学反应基座模型2.0', '--group', 'CPU资源',
            '--quota', f'0,{cpus},{memory}', '--image', image, '--image-type', 'SOURCE_OFFICIAL',
            '--instance-count', '1', '--number-of-tasks', '1', '--cpus-per-task', str(cpus),
            '--memory-per-cpu', str(memory_per_cpu), '--max-time', str(record['minutes'] / 60),
            '--no-enable-notification', '--entrypoint', entry]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['submit', 'status', 'serve'])
    parser.add_argument('--run-id')
    parser.add_argument('--state', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--max-hours', type=float, default=6)
    parser.add_argument('--image', default='slurm-gromacs:0.1.1-alpha.1')
    parser.add_argument('--root', help='Explicit scientific execution root; persisted in new receipts')
    args = parser.parse_args()
    saved = json.loads(args.state.read_text()) if args.action == 'status' and args.state else None
    configure_root(args.root, saved, required=args.action in {'submit', 'serve'})
    if args.action == 'serve':
        if args.state_dir is None or not 0 < args.max_hours <= 6:
            parser.error('serve requires --state-dir and a positive duration <=6 hours')
        args.state_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + args.max_hours * 3600
        tracked = {}
        previous = {}
        while time.monotonic() < deadline:
            try:
                for pending in remote(['pending']):
                    record = bind_record(pending)
                    path = args.state_dir / (record['run_id'] + '.json')
                    tracked[record['run_id']] = path
                    if record['receipt'] is not None and not path.exists():
                        with path.open('x') as handle:
                            json.dump(record, handle)
                    if not path.exists():
                        # submit persists its reservation before create. Any uncertain
                        # response thereafter is observed, never automatically replayed.
                        attempt = subprocess.run([sys.executable, __file__, 'submit',
                            '--run-id', record['run_id'], '--state', str(path), '--image', args.image, '--root', ROOT],
                            capture_output=True, text=True, timeout=360)
                        if attempt.returncode:
                            print(json.dumps({'run_id': record['run_id'], 'event': 'dispatch_requires_review'}), flush=True)
                            # Even preflight failures require review instead of a tight
                            # repeated create/preflight loop. Retain a local observation record.
                            if not path.exists():
                                with path.open('x') as handle:
                                    json.dump(record, handle)
                for run_id, path in list(tracked.items()):
                    result = subprocess.run([sys.executable, __file__, 'status',
                        '--run-id', run_id, '--state', str(path), '--root', ROOT], capture_output=True, text=True, timeout=240)
                    if result.returncode:
                        event = {'run_id': run_id, 'event': 'observation_unavailable_no_resubmit'}
                    else:
                        event = json.loads(result.stdout)
                    if previous.get(run_id) != event:
                        print(json.dumps(event), flush=True)
                        previous[run_id] = event
                    if event.get('scheduler_state') in {'SUCCEEDED', 'SUCCEEDED_RETAINING', 'FAILED', 'FAILED_RETAINING', 'CANCELLED', 'STOPPED', 'TIMED_OUT'}:
                        tracked.pop(run_id)
            except Exception as exc:
                print(json.dumps({'event': 'bridge_observation_error', 'type': type(exc).__name__}), flush=True)
            time.sleep(60)
        print(json.dumps({'event': 'bridge_duration_limit_reached', 'running_jobs_not_stopped': True}), flush=True)
        return
    if not args.run_id or not args.state:
        parser.error('submit/status require --run-id and --state')
    if args.action == 'submit':
        if args.state.exists():
            raise SystemExit('Submission record exists: use status, never replay submit')
        record = bind_record(remote(['inspect', args.run_id]))
        if record['receipt'] is not None or record['state'] != 'QUEUED':
            raise SystemExit('Not an unreserved queued job; inspect existing state')
        command_args = create_args(record, args.image)
        call(command_args + ['--dry-run'])
        with args.state.open('x') as handle:
            json.dump(record, handle)
        remote(['reserve', args.run_id, '--digest', record['spec_sha256']])
        # Any exception after reservation leaves the exact name for observation.
        response = call(command_args)
        if response.get('name') != record['name']:
            raise RuntimeError('Creation identity mismatch; reconcile without resubmission')
        print(json.dumps({'name': record['name'], 'submitted': True}))
        return
    record = json.loads(args.state.read_text())
    if record['run_id'] != args.run_id:
        raise SystemExit('State belongs to a different platform job')
    verify_record(record, remote(['inspect', args.run_id]))
    observed = call(['hpc', 'status', record['name'], '--workspace', 'CPU资源空间'])
    if observed['name'] != record['name']:
        raise RuntimeError('Observed workload identity mismatch')
    state = observed['status'].upper()
    failure = {'FAILED': 'FAILED', 'FAILED_RETAINING': 'FAILED',
               'CANCELLED': 'CANCELLED', 'STOPPED': 'CANCELLED', 'TIMED_OUT': 'TIMED_OUT'}.get(state)
    if failure:
        remote(['fail', args.run_id, '--digest', record['spec_sha256'],
                '--name', record['name'], '--state', failure])
    job = remote(['inspect', args.run_id])
    print(json.dumps({'name': record['name'], 'scheduler_state': state,
                      'steps': observed.get('steps'), 'platform_state': job['state']}))


if __name__ == '__main__':
    main()
