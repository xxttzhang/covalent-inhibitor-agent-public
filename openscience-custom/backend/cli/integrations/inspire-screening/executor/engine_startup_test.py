"""Isolated subprocess protocol tests, never real GPU/scientific acceptance."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import engine_startup


class EngineStartupTest(unittest.TestCase):
    def test_fixed_startup_receipts_and_failures_never_invoke_inference(self):
        for name in ('af3', 'covdock'):
            for valid in (True, False):
                with self.subTest(engine=name, valid=valid), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    tool = root / ('python' if name == 'af3' else 'run')
                    payload = {'engine': name, 'state': 'PASSED', **(
                        {'gpu_count': 1 if valid else 0, 'parameter_tensors': 2} if name == 'af3' else
                        {'vendor_license_check': valid, 'ligprep_available': True})}
                    tool.write_text('#!' + sys.executable + '\nprint("PRIVATE_DIAGNOSTIC_WARNING")\nprint(' +
                        repr('ENGINE_ADMISSION=' + json.dumps(payload)) + ')\n')
                    tool.chmod(0o755)
                    runtime = {'name': name, 'af3_python': str(tool), 'schrodinger_root': str(root),
                               'license_file': str(root / 'not-a-real-license'), 'runtime_lib_dir': str(root)}
                    plan = {'id': 'fixture', 'runtime': runtime, 'engine': {'name': name}, 'assets': {},
                            'finish_before': int(time.time() * 1000) + 10000}
                    if valid:
                        engine_startup.retain_engine_admission(plan, os.environ, root)
                    else:
                        with self.assertRaises(ValueError):
                            engine_startup.retain_engine_admission(plan, os.environ, root)
                    result = json.loads((root / 'runtime-admission.json').read_text())
                    self.assertEqual(result['state'], 'PASSED' if valid else 'FAILED')
                    self.assertFalse(result['scientific_inference_completed'])
                    self.assertNotIn('PRIVATE_DIAGNOSTIC_WARNING', json.dumps(result))
                    with self.assertRaises(FileExistsError):
                        engine_startup.retain_engine_admission(plan, os.environ, root)

    def test_expired_deadline_fails_before_spawning_and_retains_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = {'id': 'fixture', 'engine': {'name': 'af3'}, 'runtime': {'name': 'af3', 'af3_python': '/not/an/executable'},
                    'assets': {}, 'finish_before': 1}
            with self.assertRaisesRegex(ValueError, 'deadline'):
                engine_startup.retain_engine_admission(plan, os.environ, directory)
            self.assertEqual(json.loads((Path(directory) / 'runtime-admission.json').read_text())['state'], 'FAILED')


if __name__ == '__main__':
    unittest.main()
