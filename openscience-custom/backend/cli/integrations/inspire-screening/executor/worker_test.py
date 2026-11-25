"""Real filesystem/hash tests only; not chemistry or GPU acceptance."""
import importlib.util
import copy
import json
import tempfile
import unittest
import subprocess
import sys
import base64
import zlib
from pathlib import Path

spec = importlib.util.spec_from_file_location('executor_worker', Path(__file__).with_name('worker.py'))
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class EvidenceTest(unittest.TestCase):
    def test_staging_rejects_incomplete_trailing_oversized_and_changed_packets(self):
        packet = {'plan': {'fixture': 'EGFR'}, 'digest': 'a'*64}
        raw = json.dumps(packet).encode()
        valid = zlib.compress(raw)
        self.assertEqual(worker.decode_staging(base64.b64encode(valid).decode()), packet)
        for invalid in (valid[:-1], valid+b'extra', zlib.compress(b'x'*(4*1024*1024+1)), b'not-zlib'):
            with self.assertRaises(ValueError):
                worker.decode_staging(base64.b64encode(invalid).decode())
        with self.assertRaises(ValueError):
            worker.decode_staging('A'*(64*1024+1))
        with self.assertRaises(ValueError):
            worker.decode_staging(base64.b64encode(zlib.compress(b'{"unexpected":true}')).decode())
        with self.assertRaisesRegex(ValueError, 'digest/version'):
            worker.freeze_packet(worker.decode_staging(base64.b64encode(valid).decode()))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='screening-worker-test-')
        self.original = worker.ROOT
        worker.ROOT = Path(self.temp.name)
        self.task = 'screen-' + 'a' * 24
        self.expected = 'b' * 64

    def test_compressed_staging_preserves_exact_immutable_plan_and_is_idempotent(self):
        plan = {'version':'screening-executor-v1','id':self.task,'target':'EGFR','site':'C797',
            'library':'CovInDB/Virtual_Screening_Library.csv','scope':'bounded','max_records':100,
            'shortlist':3,'gates':{'min_carsidock_success':0.75,'min_rtmscore_coverage':0.95},
            'steps':[{'phase':phase,'minutes':10,'max_candidates':3} for phase in worker.PHASES],
            'runtime':{'project':str(worker.PROJECT),'root':str(worker.ROOT),
                'worker':str(Path(worker.__file__).resolve()),
                'activation':str(worker.PROJECT/'environment/activate_carsidock.sh')}}
        packet = {'plan':plan,'digest':worker.digest(plan)}
        payload = base64.b64encode(zlib.compress(json.dumps(packet).encode())).decode()
        first = worker.freeze_packet(worker.decode_staging(payload))
        path = worker.directory(self.task)/'plan.json'
        before = path.read_bytes()
        self.assertEqual(worker.freeze_packet(worker.decode_staging(payload)), first)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(json.loads(before), plan)
        self.assertEqual(first, {'staged':True,'id':self.task,'digest':packet['digest']})
        self.assertEqual({p.name for p in path.parent.iterdir()}, {'plan.json','install.lock'})

    def tearDown(self):
        worker.ROOT = self.original
        self.temp.cleanup()

    def phase(self, name, parent=None):
        dest = worker.directory(self.task) / name
        dest.mkdir(parents=True)
        artifact = dest / 'result.txt'
        artifact.write_text('test fixture, not scientific output')
        manifest = {'task_id': self.task, 'phase': name, 'plan_digest': self.expected,
                    'parent_digest': worker.digest(parent) if parent else None,
                    'status': 'SUCCEEDED', 'gates': {'fixture': True},
                    'artifacts': {'result.txt': worker.sha(artifact)}}
        worker.write(dest / 'manifest.json', manifest)
        return manifest

    def test_parent_and_real_artifacts_are_rehashed(self):
        first = self.phase('prepare')
        second = self.phase('carsidock', first)
        self.assertEqual(worker.verify_manifest(self.task, 'carsidock', self.expected), second)
        (worker.directory(self.task) / 'prepare/result.txt').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            worker.verify_manifest(self.task, 'carsidock', self.expected)

    def test_missing_artifact_is_not_success(self):
        self.phase('prepare')
        (worker.directory(self.task) / 'prepare/result.txt').unlink()
        with self.assertRaises(FileNotFoundError):
            worker.verify_manifest(self.task, 'prepare', self.expected)

    def test_parent_manifest_cannot_be_replaced(self):
        first = self.phase('prepare')
        self.phase('carsidock', first)
        first['additional_claim'] = 'different evidence'
        worker.write(worker.directory(self.task) / 'prepare/manifest.json', first)
        with self.assertRaisesRegex(ValueError, 'Parent chain'):
            worker.verify_manifest(self.task, 'carsidock', self.expected)

    def test_path_escape_and_failed_gate_are_rejected(self):
        manifest = self.phase('prepare')
        dest = worker.directory(self.task) / 'prepare'
        outside = Path(self.temp.name) / 'outside.txt'
        outside.write_text('not a task artifact')
        (dest / 'result.txt').unlink()
        (dest / 'result.txt').symlink_to(outside)
        manifest['artifacts']['result.txt'] = worker.sha(outside)
        worker.write(dest / 'manifest.json', manifest)
        with self.assertRaises(ValueError):
            worker.verify_manifest(self.task, 'prepare', self.expected)
        (dest / 'result.txt').unlink()
        (dest / 'result.txt').write_text(outside.read_text())
        manifest['gates']['fixture'] = False
        worker.write(dest / 'manifest.json', manifest)
        with self.assertRaisesRegex(ValueError, 'scientific gate'):
            worker.verify_manifest(self.task, 'prepare', self.expected)

    def test_identity_and_json_normalization(self):
        with self.assertRaises(ValueError):
            worker.directory('../outside')
        self.assertEqual(worker.canonical({'z': 1.0, 'a': ['EGFR', 0.5]}),
                         '{"a":["EGFR",0.5],"z":1}')

    def test_environment_does_not_parse_vendor_build_string(self):
        # Run the actual environment recorder in an isolated interpreter;
        # no patched platform function or synthetic package-version response.
        source = "import runpy,sys,json; module=runpy.run_path(sys.argv[1]); sys.version='vendor build with nonstandard formatting'; print(json.dumps(module['environment']('prepare')))"
        child = subprocess.run([sys.executable, '-c', source, str(Path(worker.__file__))],
                               text=True, capture_output=True, check=True)
        recorded = json.loads(child.stdout)
        self.assertEqual(recorded['python'], '.'.join(str(value) for value in sys.version_info[:3]))
        self.assertEqual(recorded['python_build'], 'vendor build with nonstandard formatting')
        self.assertIsNone(recorded['gpu'])

    def test_pilot_worker_rejects_scale_time_gpu_and_points_substitution(self):
        # Only infrastructure is synthetic. Exercise the actual worker's
        # validation boundary without loading chemistry assets or submitting.
        plan = {'version': 'screening-executor-v1', 'id': self.task,
                'target': 'EGFR', 'site': 'C797', 'library': 'CovInDB/Virtual_Screening_Library.csv',
                'scope': 'bounded', 'max_records': 20000, 'shortlist': 5, 'max_points': 1,
                'gates': {'min_carsidock_success': 0.75, 'min_rtmscore_coverage': 0.95},
                'runtime': {'project': str(worker.PROJECT), 'root': str(worker.ROOT),
                            'worker': str(Path(worker.__file__).resolve()),
                            'activation': str(worker.PROJECT / 'environment/activate_carsidock.sh')},
                'automation': {'version': 'screening-resource-planner-v1', 'selected_mode': 'research_pilot_v1',
                               'requested_mode': 'research_pilot_v1', 'adjusted': False},
                'steps': [{'phase': phase, 'minutes': minutes, 'max_candidates': 5,
                           'resource': {'quota': ('1,10,200' if phase in ('carsidock', 'rtmscore') else '0,4,16'),
                                        'points_per_hour': 1}}
                          for phase, minutes in zip(worker.PHASES, [15, 10, 5, 5])]}
        worker.validate(plan, worker.digest(plan))
        for name, value in [('max_records', 10000), ('shortlist', 6), ('max_points', None), ('max_points', 0)]:
            invalid = {**plan, name: value}
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                worker.validate(invalid, worker.digest(invalid))
        for name, value in [('minutes', 11), ('max_candidates', 6)]:
            invalid = copy.deepcopy(plan)
            invalid['steps'][1][name] = value
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                worker.validate(invalid, worker.digest(invalid))
        for name, value in [('quota', '2,20,400'), ('points_per_hour', -1), ('points_per_hour', True)]:
            invalid = copy.deepcopy(plan)
            invalid['steps'][1]['resource'][name] = value
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                worker.validate(invalid, worker.digest(invalid))
        invalid = copy.deepcopy(plan)
        invalid['automation']['selected_mode'] = 'standard'
        with self.assertRaisesRegex(ValueError, 'Research pilot'):
            worker.validate(invalid, worker.digest(invalid))


if __name__ == '__main__':
    unittest.main()
