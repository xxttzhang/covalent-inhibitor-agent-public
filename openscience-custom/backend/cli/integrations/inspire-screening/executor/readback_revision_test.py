import hashlib
import tempfile
import unittest
from pathlib import Path

from readback_revision import checked_reader


class RevisionTest(unittest.TestCase):
    def test_readers_are_bounded_hash_bound_and_not_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / 'original.py'
            repaired = Path(folder) / 'repaired.py'
            original.write_bytes(b'original')
            repaired.write_bytes(b'repaired')
            revision = dict(version='covdock-cached-coordinates-v1', plan_digest='a' * 64,
                            original_sha256=hashlib.sha256(b'original').hexdigest(),
                            path=str(repaired.resolve()), sha256=hashlib.sha256(b'repaired').hexdigest())
            before = original.stat().st_mtime_ns, repaired.stat().st_mtime_ns
            self.assertEqual(checked_reader(revision, original), repaired.resolve())
            self.assertEqual(before, (original.stat().st_mtime_ns, repaired.stat().st_mtime_ns))
            for key, value in [('sha256', 'b' * 64), ('original_sha256', 'b' * 64),
                               ('version', 'unknown'), ('path', 'relative.py'), ('plan_digest', 'invalid')]:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    checked_reader({**revision, key: value}, original)
            with self.assertRaises(ValueError):
                checked_reader({**revision, 'execute': True}, original)
            repaired.write_bytes(b'changed')
            with self.assertRaises(ValueError):
                checked_reader(revision, original)


if __name__ == '__main__':
    unittest.main()
