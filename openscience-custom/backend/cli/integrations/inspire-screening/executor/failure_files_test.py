"""Real local failure files and locks only; no engines, network or resource use."""
import fcntl
import json
import tempfile
import unittest
from pathlib import Path

import worker


class FailureFilesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='failure-evidence-')
        self.original = worker.ROOT
        worker.ROOT = Path(self.temp.name).resolve()
        self.task = 'screen-' + 'a' * 24
        self.plan = {'id': self.task, 'version': 'screening-executor-v1', 'target': 'EGFR', 'site': 'C797',
                     'library': 'CovInDB/Virtual_Screening_Library.csv', 'scope': 'bounded', 'max_records': 20,
                     'shortlist': 5, 'gates': {'min_carsidock_success': 0.75, 'min_rtmscore_coverage': 0.95},
                     'steps': [{'phase': phase, 'minutes': 1, 'max_candidates': 5} for phase in worker.PHASES],
                     'runtime': {'project': str(worker.PROJECT), 'root': str(worker.ROOT),
                                 'worker': str(Path(worker.__file__).resolve()),
                                 'activation': str(worker.PROJECT / 'environment/activate_carsidock.sh')},
                     'assets': {str(Path(worker.__file__).resolve()): worker.sha(Path(worker.__file__))}}
        self.expected = worker.digest(self.plan)
        self.dest = worker.directory(self.task) / 'carsidock'
        self.dest.mkdir(parents=True)
        worker.write(self.dest.parent / 'plan.json', self.plan)
        (self.dest / 'process.lock').touch()
        self.error = {'task_id': self.task, 'phase': 'carsidock', 'plan_digest': self.expected,
                      'status': 'FAILED', 'error': 'fixture diagnostic, not scientific evidence', 'runtime_seconds': 0.2}
        worker.write(self.dest / 'error.json', self.error)
        worker.write(self.dest / 'started.json', {'pid': 1, 'at': 10, 'plan_digest': self.expected})
        (self.dest / 'engine.log').write_text('private fixture log contents')
        (self.dest / 'unverified-poses.sdf').write_text('must not enter a validated inventory')

    def tearDown(self):
        worker.ROOT = self.original
        self.temp.cleanup()

    def read(self, witness=None):
        return worker.read_failure(self.task, 'carsidock', self.expected, witness)

    def test_exact_read_only_witness_excludes_contents_and_unverified_molecules(self):
        before = {path.name: path.read_bytes() for path in self.dest.iterdir()}
        value = self.read()
        self.assertEqual(self.read(worker.digest(value)), value)
        self.assertEqual(set(value['artifacts']), {'error.json', 'started.json', 'engine.log'})
        self.assertEqual(value['artifacts']['engine.log']['sha256'], worker.sha(self.dest / 'engine.log'))
        self.assertNotIn('private fixture', json.dumps(value))
        self.assertNotIn(self.error['error'], json.dumps(value))
        self.assertFalse(value['scientific_quality_verified'])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.dest.iterdir()})

    def test_changed_and_added_diagnostics_cannot_replace_retained_witness(self):
        witness = worker.digest(self.read())
        (self.dest / 'engine.log').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'retained witness'):
            self.read(witness)
        witness = worker.digest(self.read())
        (self.dest / 'environment.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'retained witness'):
            self.read(witness)

    def test_active_worker_missing_error_and_success_manifest_are_not_failure_evidence(self):
        with (self.dest / 'process.lock').open('r') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.read()
        (self.dest / 'manifest.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'success manifest'):
            self.read()
        (self.dest / 'manifest.json').unlink()
        (self.dest / 'error.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.read()

    def test_wrong_identity_invalid_timing_and_changed_plan_are_rejected(self):
        for change in ({'task_id': 'other'}, {'phase': 'report'}, {'plan_digest': '0' * 64},
                       {'status': 'SUCCEEDED'}, {'error': ''}, {'runtime_seconds': -1}, {'runtime_seconds': True}):
            with self.subTest(change=change):
                worker.write(self.dest / 'error.json', {**self.error, **change})
                with self.assertRaises(ValueError):
                    self.read()
        worker.write(self.dest / 'error.json', self.error)
        worker.write(self.dest.parent / 'plan.json', {**self.plan, 'shortlist': 4})
        with self.assertRaisesRegex(ValueError, 'digest/version'):
            self.read()

    def test_symlinks_and_oversized_files_are_rejected_without_reading_them(self):
        path = self.dest / 'engine.log'
        path.unlink()
        path.symlink_to(self.dest / 'error.json')
        with self.assertRaisesRegex(ValueError, 'regular file'):
            self.read()
        path.unlink()
        with path.open('wb') as stream:
            stream.truncate(128 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, 'bounded regular'):
            self.read()
        path.unlink()
        parent = self.dest.parent
        original = parent / 'moved'
        self.dest.rename(original)
        self.dest.symlink_to(original, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'redirected'):
            self.read()


if __name__ == '__main__':
    unittest.main()
