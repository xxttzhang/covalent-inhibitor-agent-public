"""Durable, bounded one-sentence de novo worker. No model-generated shell commands."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.replace(temp, path)


def unpack(archive, dest):
    with zipfile.ZipFile(archive) as z:
        names = set()
        if sum(i.file_size for i in z.infolist()) > 128 * 1024 * 1024:
            raise RuntimeError('Output archive exceeds limit')
        for item in z.infolist():
            p = Path(item.filename)
            if p.is_absolute() or '..' in p.parts or item.filename in names or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError('Unsafe or duplicate archive entry')
            names.add(item.filename)
        z.extractall(dest)
    manifest = json.loads((dest / 'manifest.json').read_text())
    actual = {str(p.relative_to(dest)) for p in dest.rglob('*') if p.is_file()}
    if actual != set(manifest) | {'manifest.json'}:
        raise RuntimeError('Output manifest inventory differs')
    for name, digest in manifest.items():
        if sha(dest / name) != digest:
            raise RuntimeError('Downloaded artifact hash differs')


class Worker:
    def __init__(self, root):
        self.root = root
        self.request = json.loads((root / 'request.json').read_text())
        self.package = root / 'package'
        self.profile = json.loads((self.package / 'profile.json').read_text())
        self.remote = self.profile['remote_root'] + '/' + self.request['id']
        self.name = self.request['id']

    def state(self, state, **extra):
        save(self.root / 'status.json', dict(id=self.name, state=state, updated=time.time(), job_name=self.name, requested_slots=self.request['slots'], **extra))

    def cli(self, *args, timeout=120):
        result = subprocess.run([self.profile['cli'], '--json', '--no-env-file', '--account', self.profile['account'], *args], capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError('Inspire command failed: ' + ' '.join(args[:2]) + ' ' + result.stderr[-1800:])
        data = json.loads(result.stdout)
        if data.get('success') is not True:
            raise RuntimeError('Inspire returned an unsuccessful response')
        return data['data']

    def remote_call(self, argv):
        result = self.cli('notebook', 'exec', self.profile['notebook'], '--workspace', self.profile['notebook_workspace'], shlex.join(argv))
        if result['returncode']:
            raise RuntimeError('Remote preparation failed: ' + result['output'][-1500:])
        return result['output']

    def copy(self, source, dest, download=False, recursive=False):
        return self.cli('notebook', 'scp', self.profile['notebook'], '--workspace', self.profile['notebook_workspace'], *(['--download'] if download else []), *(['--recursive'] if recursive else []), str(source), str(dest), timeout=300)

    def api(self, route, body=None, raw=False):
        port = 4096
        runtime = os.environ.get('COVALENT_RUNTIME_ROOT')
        if runtime:
            binding = json.loads((Path(runtime) / 'binding.json').read_text())
            port = binding['backend_port']
            if (type(port) is not int or not 1 <= port <= 65535
                    or binding.get('project') != self.request['directory']):
                raise RuntimeError('Results backend binding differs from the task project')
        url = f'http://127.0.0.1:{port}' + route + ('&' if '?' in route else '?') + urllib.parse.urlencode({'directory': self.request['directory']})
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30) as response:
            value = response.read()
            return value if raw else json.loads(value)

    def publish(self):
        dest = Path(self.request['directory']) / 'denovo-results' / self.name
        dest.mkdir(parents=True, exist_ok=True)
        ledger_path = self.root / 'publication.json'
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        for name in ['report.md', 'summary.json', 'verification.json', 'candidates.csv', 'raw_accepted.sdf', 'relaxed_accepted.sdf', 'candidates.png']:
            source = self.root / 'acceptance' / name
            if not source.is_file() or source.stat().st_size == 0:
                continue
            target = dest / name
            if target.exists() and sha(target) != sha(source):
                raise RuntimeError('Existing delivered artifact differs')
            if not target.exists():
                shutil.copy2(source, target)
            active = self.api('/file/artifact-store')
            matches = [a for a in active if a.get('current', {}).get('sourcePath') == str(target)]
            if len(matches) > 1:
                raise RuntimeError('Ambiguous existing artifact')
            if matches:
                artifact = matches[0]
            else:
                if name in ledger:
                    raise RuntimeError('Publication unresolved or removed; never duplicate automatically')
                ledger[name] = {'state': 'RESERVED', 'source': str(target), 'sha256': sha(target)}
                save(ledger_path, ledger)
                artifact = self.api('/file/artifact', dict(path=str(target), sessionID=self.request['session_id'], summary='de novo ' + self.name + ' · ' + name))
            downloaded = self.api('/file/artifact-store/' + artifact['id'] + '/raw?versionID=' + artifact['currentVersionID'], raw=True)
            if hashlib.sha256(downloaded).hexdigest() != sha(target):
                raise RuntimeError('Results readback differs')
            ledger[name] = dict(state='VERIFIED', artifact_id=artifact['id'], version_id=artifact['currentVersionID'], source=str(target), sha256=sha(target))
            save(ledger_path, ledger)
        return ledger

    def verify_acceptance(self):
        verifier = self.package / 'verify_acceptance.py'
        if self.request['package_hashes'].get('verify_acceptance.py') != sha(verifier):
            raise RuntimeError('Acceptance verifier is not bound to the frozen request')
        # Revalidate even on resume: accepted.json alone cannot authorize delivery.
        env = os.environ.copy()
        env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
        with (self.root / 'acceptance.log').open('a') as log:
            subprocess.run([self.profile['local_python'], str(verifier), str(self.root),
                '--request-sha256', sha(self.root / 'request.json'),
                '--output', str(self.root / 'acceptance' / 'verification.json')],
                check=True, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1800)

    def verify_completed(self):
        self.verify_acceptance()
        completed = json.loads((self.root / 'complete.json').read_text())
        summary = json.loads((self.root / 'acceptance' / 'summary.json').read_text())
        if completed.get('summary') != summary:
            raise RuntimeError('Completion summary differs from validated results')
        # Re-read delivered bytes too; a completion marker is not provenance.
        artifacts = self.publish()
        if completed.get('artifacts') != artifacts:
            raise RuntimeError('Completion artifacts differ from verified publication')
        self.state('COMPLETED', **completed)

    def observe_generation(self):
        p = self.profile
        observed = self.cli('job', 'status', self.name, '--workspace', p['workspace'])
        if observed['name'] != self.name or observed.get('project') != p['project'] or observed.get('compute_group') != p['group']:
            raise RuntimeError('Observed job scope differs')
        resource = observed['resource']
        if (resource['gpu'], resource['cpu'], resource['memory_gib'], resource['nodes']) != (1, 10, 200, 1):
            raise RuntimeError('Observed job resources differ')
        save(self.root / 'job_status.json', observed)
        return observed['status'].upper()

    def wait_generation(self):
        submission = json.loads((self.root / 'submission.json').read_text())
        reserved_at = submission.get('time')
        if (submission.get('name') != self.name or submission.get('state') not in {'SUBMITTING', 'SUBMITTED'}
                or type(reserved_at) not in (int, float) or not math.isfinite(reserved_at) or reserved_at <= 0):
            raise RuntimeError('Original submission observation identity or timestamp is invalid')
        # A recovered worker keeps the original observation budget and job handle.
        deadline = reserved_at + 3600
        stop_path = self.root / 'stop-request.json'
        binding = {'name': self.name, 'request_sha256': sha(self.root / 'request.json')}
        if stop_path.exists():
            stop = json.loads(stop_path.read_text())
            if any(stop.get(key) != value for key, value in binding.items()):
                raise RuntimeError('Stop request differs from the original generation task')
        while True:
            try:
                state = self.observe_generation()
            except Exception as error:
                if not stop_path.exists():
                    raise
                self.state('NEEDS_REVIEW', error='Stop was requested, but the remote terminal state is unknown: ' + str(error),
                    stop_requested=True, remote_stop_confirmed=False)
                return False
            if state in {'SUCCEEDED', 'SUCCEEDED_RETAINING'}:
                # Completion may race a stop request; validate the actual output.
                return True
            if state in {'FAILED', 'FAILED_RETAINING', 'STOPPED', 'JOB_STOPPED', 'CANCELLED', 'TIMED_OUT'}:
                cancelled = state in {'STOPPED', 'JOB_STOPPED', 'CANCELLED'}
                self.state('CANCELLED' if cancelled else 'FAILED', error='GPU job ' + state,
                    remote_status=state, stop_requested=stop_path.exists(), remote_stop_confirmed=cancelled)
                return False
            if stop_path.exists():
                # Neither an accepted stop response nor a local marker proves exit.
                # A resumed worker observes this same handle; it never sends stop again.
                self.state('NEEDS_REVIEW', error='Stop was requested, but the scheduler has not confirmed termination; inspect the original job.',
                    remote_status=state, stop_requested=True, remote_stop_confirmed=False)
                return False
            if state not in {'RUNNING', 'PENDING', 'QUEUING'}:
                self.state('NEEDS_REVIEW', error='Scheduler state is not recognized as active or terminal; inspect the original job without resubmission.',
                    remote_status=state, stop_requested=False, remote_stop_confirmed=False)
                return False
            if (self.root / 'cancel').exists() or time.time() > deadline:
                stop = {**binding, 'requested_at': time.time(),
                    'reason': 'user_cancel' if (self.root / 'cancel').exists() else 'observation_deadline',
                    'observation_deadline': deadline}
                # Reserve before the external mutation. An uncertain response cannot
                # authorize a second stop or a new generation on recovery.
                save(stop_path, stop)
                try:
                    response = self.cli('job', 'stop', self.name, '--workspace', self.profile['workspace'])
                    save(self.root / 'stop-response.json', response)
                except Exception as error:
                    save(self.root / 'stop-error.json', {'error': str(error)})
                continue
            self.state('GENERATING' if state == 'RUNNING' else 'QUEUED', remote_status=state)
            time.sleep(15)

    def run(self):
        p = self.profile
        approval = json.loads((self.root / 'authorization.json').read_text())
        if approval['request_sha256'] != sha(self.root / 'request.json') or approval['session_id'] != self.request['session_id'] or approval['user_message_id'] != self.request['user_message_id']:
            raise RuntimeError('Native approval does not match this immutable request')
        if sha(self.root / 'worker.py') != self.request['worker_sha256']:
            raise RuntimeError('Approved worker changed')
        if self.request['directory'] not in p['allowed_directories'] or not 1 <= self.request['slots'] <= p['max_slots']:
            raise RuntimeError('Request is outside registered scope')
        for name, expected in self.request['package_hashes'].items():
            if sha(self.package / name) != expected:
                raise RuntimeError('Pinned package changed')
        if (self.root / 'complete.json').exists():
            self.state('VALIDATING')
            self.verify_completed()
            return
        self.state('PREPARING')
        submitted = self.root / 'submission.json'
        if not submitted.exists():
            quota = self.cli('job', 'quota', '--workspace', p['workspace'], '--group', p['group'], '--all')['items']
            row = next((r for r in quota if r['compute_group'] == p['group'] and r['quota'] == p['quota']), None)
            if not row or row.get('points_per_hour') is None or row['points_per_hour'] > p['max_points_per_hour']:
                raise RuntimeError('Live quota/rate exceeds registered bounds')
            save(self.root / 'live_quota.json', row)
            images = self.cli('image', 'list', '--workspace', p['workspace'], '--source', 'official', '--keyword', 'ngc-pytorch')['items']
            if not any(i['name'] == p['image'] and i['status'] in ['SUCCESS', 'READY'] for i in images):
                raise RuntimeError('Registered image is unavailable')
            self.remote_call(['mkdir', '-p', p['remote_root']])
            self.remote_call([p['python'], '-c', 'from pathlib import Path; import sys; p=Path(sys.argv[1]); p.mkdir(exist_ok=False)', self.remote]) if not (self.root / 'staging.json').exists() else None
            save(self.root / 'staging.json', {'remote': self.remote})
            for path in self.package.iterdir():
                self.copy(path, self.remote + '/' + path.name, recursive=path.is_dir())
            self.copy(self.root / 'request.json', self.remote + '/request.json')
            # Verify pinned model files on the CPU notebook before reserving a GPU
            # job. generate.py repeats this check inside the job to detect changes.
            preflight = json.loads(self.remote_call(
                [p['python'], self.remote + '/generate.py', self.remote, '--preflight']))
            if (preflight.get('state') != 'ASSETS_VERIFIED'
                    or preflight.get('request_sha256') != sha(self.root / 'request.json')
                    or preflight.get('repo_commit') != p['repo_commit']
                    or preflight.get('external_hashes') != p['external_hashes']
                    or preflight.get('compute_submitted') is not False):
                raise RuntimeError('Model asset preflight differs from frozen request')
            save(self.root / 'asset_preflight.json', preflight)
            command = shlex.join([p['python'], self.remote + '/generate.py', self.remote])
            args = ['job', 'create', '--name', self.name, '--workspace', p['workspace'], '--project', p['project'], '--group', p['group'], '--quota', p['quota'], '--image', p['image'], '--priority', '4', '--nodes', '1', '--shm-size', '8', '--max-time', str(p['max_hours']), '--no-enable-notification', '--no-auto-fault-tolerance', '--command', command]
            save(self.root / 'dry_run.json', self.cli(*args, '--dry-run'))
            if (self.root / 'cancel').exists():
                self.state('CANCELLED')
                return
            # Reserve BEFORE submission; any uncertain outcome is observed, never resubmitted.
            reserved_at = time.time()
            save(submitted, dict(name=self.name, state='SUBMITTING', time=reserved_at))
            self.state('SUBMITTING')
            try:
                result = self.cli(*args)
                if result['name'] != self.name:
                    raise RuntimeError('Wrong submitted job name')
                save(submitted, dict(name=self.name, state='SUBMITTED', response=result, time=reserved_at))
            except Exception as error:
                save(self.root / 'submission_error.json', {'error': str(error)})
        if not self.wait_generation():
            return
        self.state('DOWNLOADING')
        archive = self.root / 'output.zip'
        evidence = json.loads(self.remote_call([p['python'], '-c', 'import hashlib,json,pathlib,sys; p=pathlib.Path(sys.argv[1]); assert p.is_file() and not p.is_symlink() and p.stat().st_size<=64*1024*1024; print(json.dumps(dict(sha256=hashlib.sha256(p.read_bytes()).hexdigest(),size=p.stat().st_size)))', self.remote + '/output.zip']))
        if not archive.exists():
            self.copy(self.remote + '/output.zip', str(archive) + '.part', download=True)
            os.replace(str(archive) + '.part', archive)
        if sha(archive) != evidence['sha256']:
            raise RuntimeError('Archive integrity failed')
        save(self.root / 'download_evidence.json', evidence)
        unpack(archive, self.root / 'download')
        self.state('VALIDATING')
        output = self.root / 'acceptance'
        if output.exists() and not (self.root / 'accepted.json').exists():
            output.rename(self.root / ('acceptance_interrupted_' + str(time.time_ns())))
        if not (self.root / 'accepted.json').exists():
            env = os.environ.copy()
            env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1')
            with (self.root / 'acceptance.log').open('a') as log:
                subprocess.run([p['local_python'], str(self.package / 'accept.py'), str(self.root)], check=True, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
            save(self.root / 'accepted.json', {'summary_sha256': sha(output / 'summary.json')})
        self.verify_acceptance()
        self.state('PUBLISHING')
        artifacts = self.publish()
        result = dict(summary=json.loads((output / 'summary.json').read_text()), artifacts=artifacts)
        save(self.root / 'complete.json', result)
        self.state('COMPLETED', **result)


def main(root):
    # OS releases locks on crashes. Resume uses the same durable submission handle.
    with (root / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        worker = Worker(root)
        try:
            with (root.parent / 'capacity.lock').open('a') as capacity:
                worker.state('WAITING_FOR_CAPACITY')
                fcntl.flock(capacity, fcntl.LOCK_EX)
                worker.run()
        except Exception as error:
            worker.state('NEEDS_REVIEW', error=str(error))
            raise


if __name__ == '__main__':
    main(Path(sys.argv[1]).resolve())
