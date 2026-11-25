"""Exercise durable stop/recovery with real task files and a scripted scheduler.

No Inspire command, model, GPU or scientific evaluation is executed.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

WORKER_PATH = Path(__file__).resolve().parents[1] / 'runtime/worker.py'
spec = importlib.util.spec_from_file_location('denovo_stop_worker', WORKER_PATH)
worker_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker_module)


class StopRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / ('denovo-' + 'a' * 24)
        self.root.mkdir()
        package = self.root / 'package'
        package.mkdir()
        self.profile = {'remote_root': '/test/remote', 'workspace': 'test-workspace',
            'project': 'test-project', 'group': 'test-group', 'max_slots': 1,
            'allowed_directories': [self.temp.name]}
        worker_module.save(package / 'profile.json', self.profile)
        (self.root / 'worker.py').write_bytes(WORKER_PATH.read_bytes())
        self.request = {'id': self.root.name, 'slots': 1, 'session_id': 'test-session',
            'user_message_id': 'test-message', 'directory': self.temp.name,
            'worker_sha256': worker_module.sha(self.root / 'worker.py'),
            'package_hashes': {'profile.json': worker_module.sha(package / 'profile.json')}}
        worker_module.save(self.root / 'request.json', self.request)
        worker_module.save(self.root / 'authorization.json', {
            'request_sha256': worker_module.sha(self.root / 'request.json'),
            'session_id': 'test-session', 'user_message_id': 'test-message'})
        worker_module.save(self.root / 'submission.json', {'name': self.root.name,
            'state': 'SUBMITTED', 'time': 1000})

    def status(self, state, **changes):
        return {'name': self.root.name, 'project': self.profile['project'],
            'compute_group': self.profile['group'], 'status': state,
            'resource': {'gpu': 1, 'cpu': 10, 'memory_gib': 200, 'nodes': 1}, **changes}

    def scheduler(self, observations, stop_error=None):
        worker = worker_module.Worker(self.root)
        observations = iter(observations)
        calls = []

        def cli(*args, **kwargs):
            calls.append(args)
            self.assertEqual(args[0], 'job')
            self.assertEqual(args[2:], (self.root.name, '--workspace', self.profile['workspace']))
            if args[1] == 'status':
                result = next(observations)
                if isinstance(result, Exception):
                    raise result
                return result
            self.assertEqual(args[1], 'stop', 'Recovery must never create or prepare a job')
            # This is checked at the external mutation boundary, before response.
            receipt = self.read('stop-request.json')
            self.assertEqual(receipt['name'], self.root.name)
            self.assertEqual(receipt['request_sha256'], worker_module.sha(self.root / 'request.json'))
            if stop_error:
                raise stop_error
            return {'name': self.root.name, 'stop_requested': True}

        worker.cli = cli
        return worker, calls

    def read(self, filename):
        return json.loads((self.root / filename).read_text())

    def test_accepted_stop_is_not_terminal_and_restart_never_repeats_mutation(self):
        (self.root / 'cancel').write_text('cancel requested')
        worker, calls = self.scheduler([self.status('RUNNING'), self.status('RUNNING')])
        worker.run()
        self.assertEqual([call[1] for call in calls], ['status', 'stop', 'status'])
        current = self.read('status.json')
        self.assertEqual(current['state'], 'NEEDS_REVIEW')
        self.assertFalse(current['remote_stop_confirmed'])
        self.assertEqual(current['remote_status'], 'RUNNING')
        for observed, expected in [('RUNNING', 'NEEDS_REVIEW'), ('STOPPED', 'CANCELLED')]:
            resumed, calls = self.scheduler([self.status(observed)])
            resumed.run()
            self.assertEqual([call[1] for call in calls], ['status'])
            self.assertEqual(self.read('status.json')['state'], expected)
        self.assertTrue(self.read('status.json')['remote_stop_confirmed'])
        self.assertFalse((self.root / 'complete.json').exists())

    def test_uncertain_stop_and_failed_observation_remain_unknown(self):
        (self.root / 'cancel').write_text('cancel requested')
        worker, calls = self.scheduler([self.status('QUEUING'), RuntimeError('transport lost')],
            stop_error=TimeoutError('stop response lost'))
        self.assertFalse(worker.wait_generation())
        self.assertEqual([call[1] for call in calls], ['status', 'stop', 'status'])
        self.assertIn('stop response lost', self.read('stop-error.json')['error'])
        self.assertEqual(self.read('status.json')['state'], 'NEEDS_REVIEW')
        self.assertFalse(self.read('status.json')['remote_stop_confirmed'])
        resumed, calls = self.scheduler([self.status('CANCELLED')])
        self.assertFalse(resumed.wait_generation())
        self.assertEqual([call[1] for call in calls], ['status'])
        self.assertEqual(self.read('status.json')['state'], 'CANCELLED')

    def test_recovery_uses_original_deadline_without_granting_another_hour(self):
        worker, calls = self.scheduler([self.status('QUEUING'), self.status('STOPPED')])
        with patch.object(worker_module.time, 'time', return_value=5000):
            self.assertFalse(worker.wait_generation())
        self.assertEqual([call[1] for call in calls], ['status', 'stop', 'status'])
        stop = self.read('stop-request.json')
        self.assertEqual(stop['reason'], 'observation_deadline')
        self.assertEqual(stop['observation_deadline'], 4600)

    def test_success_racing_stop_continues_to_result_validation(self):
        (self.root / 'cancel').write_text('cancel requested')
        worker, calls = self.scheduler([self.status('RUNNING'), self.status('SUCCEEDED')])
        self.assertTrue(worker.wait_generation())
        self.assertEqual([call[1] for call in calls], ['status', 'stop', 'status'])
        self.assertFalse((self.root / 'complete.json').exists())
        self.assertFalse((self.root / 'status.json').exists())

    def test_live_job_before_deadline_is_polled_without_stop(self):
        worker, calls = self.scheduler([self.status('RUNNING'), self.status('SUCCEEDED_RETAINING')])
        with patch.object(worker_module.time, 'time', return_value=2000), \
                patch.object(worker_module.time, 'sleep') as sleep:
            self.assertTrue(worker.wait_generation())
        sleep.assert_called_once_with(15)
        self.assertEqual([call[1] for call in calls], ['status', 'status'])
        self.assertEqual(self.read('status.json')['state'], 'GENERATING')

    def test_only_explicit_pending_states_are_presented_as_queued(self):
        for state in ('PENDING', 'QUEUING'):
            with self.subTest(state=state):
                worker, calls = self.scheduler([self.status(state), self.status('SUCCEEDED')])
                with patch.object(worker_module.time, 'time', return_value=2000), \
                        patch.object(worker_module.time, 'sleep'):
                    self.assertTrue(worker.wait_generation())
                self.assertEqual([call[1] for call in calls], ['status', 'status'])
                self.assertEqual(self.read('status.json')['state'], 'QUEUED')
                self.assertEqual(self.read('status.json')['remote_status'], state)

    def test_unknown_first_observation_never_claims_queued_or_requests_stop(self):
        # Even an expired observation budget cannot turn unknown state into liveness.
        for state in ('UNKNOWN', 'UNRECOGNIZED_STATE'):
            with self.subTest(state=state):
                worker, calls = self.scheduler([self.status(state)])
                with patch.object(worker_module.time, 'time', return_value=5000):
                    worker.run()
                current = self.read('status.json')
                self.assertEqual(current['state'], 'NEEDS_REVIEW')
                self.assertEqual(current['remote_status'], state)
                self.assertFalse(current['remote_stop_confirmed'])
                self.assertEqual([call[1] for call in calls], ['status'])
                self.assertFalse((self.root / 'stop-request.json').exists())

    def test_restart_keeps_unknown_status_and_original_job_without_mutation(self):
        worker_module.save(self.root / 'status.json', {'id': self.root.name,
            'state': 'NEEDS_REVIEW', 'remote_status': 'UNKNOWN'})
        original = (self.root / 'submission.json').read_bytes()
        for _ in range(2):
            worker, calls = self.scheduler([self.status('UNKNOWN')])
            worker.run()
            self.assertEqual([call[1] for call in calls], ['status'])
            self.assertEqual(self.read('status.json')['state'], 'NEEDS_REVIEW')
            self.assertEqual(self.read('status.json')['remote_status'], 'UNKNOWN')
            self.assertEqual((self.root / 'submission.json').read_bytes(), original)
        self.assertFalse((self.root / 'stop-request.json').exists())

    def test_scheduler_failure_is_preserved_without_stop_or_create(self):
        worker, calls = self.scheduler([self.status('TIMED_OUT')])
        self.assertFalse(worker.wait_generation())
        self.assertEqual([call[1] for call in calls], ['status'])
        self.assertEqual(self.read('status.json')['state'], 'FAILED')
        self.assertEqual(self.read('status.json')['remote_status'], 'TIMED_OUT')
        self.assertFalse(self.read('status.json')['remote_stop_confirmed'])

    def test_stop_confirmation_rejects_another_jobs_terminal_state(self):
        (self.root / 'cancel').write_text('cancel requested')
        worker, calls = self.scheduler([self.status('RUNNING'), self.status('STOPPED', name='another-job')])
        self.assertFalse(worker.wait_generation())
        self.assertEqual(self.read('status.json')['state'], 'NEEDS_REVIEW')
        self.assertFalse(self.read('status.json')['remote_stop_confirmed'])

    def test_changed_stop_or_submission_identity_never_reaches_scheduler(self):
        worker_module.save(self.root / 'stop-request.json', {
            'name': self.root.name, 'request_sha256': '0' * 64})
        worker, calls = self.scheduler([])
        with self.assertRaisesRegex(RuntimeError, 'Stop request differs'):
            worker.wait_generation()
        self.assertEqual(calls, [])
        (self.root / 'stop-request.json').unlink()
        for record in ({'name': 'another-job', 'state': 'SUBMITTED', 'time': 1000},
                       {'name': self.root.name, 'state': 'SUBMITTED', 'time': float('nan')},
                       {'name': self.root.name, 'state': 'SUBMITTED', 'time': None}):
            worker_module.save(self.root / 'submission.json', record)
            with self.assertRaisesRegex(RuntimeError, 'submission observation identity or timestamp'):
                worker.wait_generation()
            self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
