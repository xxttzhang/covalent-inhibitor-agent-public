import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from denovo_evidence_transfer import install, unpack

ID = 'hyp-'+'a'*24

def package(change=None):
    files={'evidence.json':b'{"fixture":true}', 'pdbs/fixture.pdb':b'SOFTWARE TEST ONLY\n'}
    manifest={'files':{n:hashlib.sha256(v).hexdigest() for n,v in files.items()}}
    raw=json.dumps(manifest).encode();files['manifest.json']=raw
    if change:change(files)
    output=io.BytesIO()
    with tarfile.open(fileobj=output,mode='w') as archive:
        for name,data in files.items():
            entry=tarfile.TarInfo(name);entry.size=len(data);archive.addfile(entry,io.BytesIO(data))
    return output.getvalue(),hashlib.sha256(raw).hexdigest()

class EvidenceTransfer(unittest.TestCase):
    def test_actual_bytes_replay_and_preserve_existing_proposals(self):
        blob,digest=package()
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/ID/'proposal-fixture';p.mkdir(parents=True);(p/'report.md').write_text('preserve')
            first=install(blob,ID,digest,directory)
            self.assertEqual(install(blob,ID,digest,directory),first)
            self.assertEqual((p/'report.md').read_text(),'preserve')
            self.assertEqual(first['file_count'],3)
            self.assertFalse(first['compute_submitted'])

    def test_changed_evidence_rejected(self):
        blob,digest=package(lambda files:files.update({'evidence.json':b'{"changed":true}'}))
        with self.assertRaisesRegex(ValueError,'checksum differs'):unpack(blob,digest)

    def test_unsealed_file_rejected(self):
        blob,digest=package(lambda files:files.update({'extra.json':b'{}'}))
        with self.assertRaisesRegex(ValueError,'inventory differs'):unpack(blob,digest)

    def test_traversal_rejected(self):
        blob,digest=package(lambda files:files.update({'../outside.json':b'{}'}))
        with self.assertRaisesRegex(ValueError,'filename'):unpack(blob,digest)

    def test_conflicting_existing_evidence_is_preserved(self):
        blob,digest=package()
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/ID;p.mkdir();(p/'evidence.json').write_text('original')
            with self.assertRaisesRegex(ValueError,'Existing immutable'):install(blob,ID,digest,directory)
            self.assertEqual((p/'evidence.json').read_text(),'original')

    def test_symlink_destination_rejected(self):
        blob,digest=package()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/ID).symlink_to(root)
            with self.assertRaisesRegex(ValueError,'symbolic links'):install(blob,ID,digest,directory)

    def test_nested_symlink_rejected_without_writing_outside(self):
        blob,digest=package()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/ID).mkdir();outside=root/'outside';outside.mkdir()
            (root/ID/'pdbs').symlink_to(outside)
            with self.assertRaisesRegex(ValueError,'symbolic links'):install(blob,ID,digest,root)
            self.assertEqual(list(outside.iterdir()),[])

if __name__=='__main__':unittest.main()
