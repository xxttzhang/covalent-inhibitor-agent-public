"""Early provenance failures use real files, without launching any model."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

PACKAGE = Path(__file__).resolve().parents[1] / 'registry/egfr_c797_v1'
sys.path.insert(0, str(PACKAGE))
from verify_acceptance import verify, inside


class Verification(unittest.TestCase):
    def test_request_mutation_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'request.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'Frozen request hash'):
                verify(root, '0' * 64)

    def test_unknown_profile_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = json.dumps({'profile': 'unknown'}).encode()
            (root / 'request.json').write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'Unsupported generation profile'):
                verify(root, hashlib.sha256(data).hexdigest())

    def test_changed_evaluator_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = json.dumps({'profile': 'egfr-c797-acrylamide-v1', 'slots': 8,
                               'package_hashes': {'evaluator.py': '0' * 64}}).encode()
            (root / 'request.json').write_bytes(data)
            with self.assertRaisesRegex(ValueError, 'Evaluator/input version differs'):
                verify(root, hashlib.sha256(data).hexdigest())

    def test_path_escape_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'escapes task'):
                inside(Path(directory), '../outside')


if __name__ == '__main__':
    unittest.main()
