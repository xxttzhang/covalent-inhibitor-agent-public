"""Run the receiver locally against artificial bytes, never SSH or scientific jobs."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

file = Path(__file__).resolve().parents[1]/'runtime/review_transport.py'
spec = importlib.util.spec_from_file_location('transport_under_test', file)
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


def archive(files, names=None):
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    stream = io.BytesIO()
    stream.write((json.dumps({'files': hashes})+'\n').encode())
    with tarfile.open(fileobj=stream, mode='w|') as tar:
        for name in names or files:
            member = tarfile.TarInfo(name);member.size=len(files[name]);member.mode=0o600
            tar.addfile(member, io.BytesIO(files[name]))
    return hashes, stream.getvalue()


class Transport(unittest.TestCase):
    def test_atomic_transport_repeat_and_existing_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            files = {'source.json': json.dumps({'schema': 'native-denovo-independent-source-v1',
                'route': 'de-novo', 'study_id': 'artificial-study'}).encode(),
                'independent/candidate.sdf': b'artificial transport-only bytes'}
            hashes, payload = archive(files)
            identity = 'denovo-review-'+hashes['source.json'][:32]
            command = [sys.executable, '-B', '-c', module.REMOTE, str(root), identity, hashes['source.json']]
            first = subprocess.run(command, input=payload, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr.decode())
            second = subprocess.run(command, input=payload, capture_output=True)
            self.assertEqual(second.returncode, 0, second.stderr.decode())
            result = json.loads(second.stdout)
            self.assertFalse(result['compute_submitted'])
            (root/'state/de-novo-inputs'/identity/'independent/candidate.sdf').write_bytes(b'changed')
            third = subprocess.run(command, input=payload, capture_output=True)
            self.assertNotEqual(third.returncode, 0)
            self.assertIn(b'changed', third.stderr)
            self.assertFalse(list((root/'state/de-novo-inputs').glob('.incoming-*')))

    def test_traversal_and_duplicate_members_rejected(self):
        for name, duplicate in [('../outside', False), ('input', True)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                files = {'source.json': b'{}', name: b'bytes'}
                hashes, payload = archive(files, ['source.json', name, name] if duplicate else None)
                done = subprocess.run([sys.executable, '-B', '-c', module.REMOTE, str(root),
                    'denovo-review-'+hashes['source.json'][:32], hashes['source.json']], input=payload, capture_output=True)
                self.assertNotEqual(done.returncode, 0)
                self.assertFalse((root.parent/'outside').exists())

if __name__ == '__main__': unittest.main()
