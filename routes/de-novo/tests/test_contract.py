"""Failure-boundary checks using real archives and RDKit molecules."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from rdkit import Chem

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'registry/egfr_c797_v1'))
from accept import identity_failures
sys.path.insert(0, str(ROOT / 'runtime'))
from worker import unpack


class Contract(unittest.TestCase):
    def test_provenance(self):
        mol = Chem.MolFromSmiles('CNC(=O)C=C')
        self.assertEqual(identity_failures(mol, {'smiles': 'C=CC(=O)NC'}), [])
        self.assertIn('sdf_log_identity_mismatch', identity_failures(mol, {'smiles': 'CCC'}))
        self.assertIn('upstream_reconstruction_failed', identity_failures(mol, {'smiles': 'C=CC(=O)NC', 'tag': 'bad'}))

    def test_stereochemistry_remains_strict(self):
        mol = Chem.MolFromSmiles('C[C@H](N)C(=O)O')
        self.assertIn('sdf_log_identity_mismatch', identity_failures(mol, {'smiles': 'CC(N)C(=O)O'}))

    def test_unsafe_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(root / 'bad.zip', 'w') as archive:
                archive.writestr('../escaped', 'bad')
            with self.assertRaisesRegex(RuntimeError, 'Unsafe'):
                unpack(root / 'bad.zip', root / 'out')
            self.assertFalse((root / 'escaped').exists())

    def test_corrupt_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with zipfile.ZipFile(root / 'bad.zip', 'w') as archive:
                archive.writestr('manifest.json', '{}')
                archive.writestr('unlisted.sdf', 'bad')
            with self.assertRaisesRegex(RuntimeError, 'inventory'):
                unpack(root / 'bad.zip', root / 'out')


if __name__ == '__main__':
    unittest.main()
