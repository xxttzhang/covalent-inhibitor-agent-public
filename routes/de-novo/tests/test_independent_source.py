"""Chemical input and source boundary tests; no generator/docking/MD is run."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from rdkit import Chem
from rdkit.Chem import AllChem

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'evaluation'))
from source import (REGISTRY, PRECURSOR_SHA256, molecule_row, regular, inventory,
                    table_bytes, task_binding, validate_control)
from control import prepare_control


class SourceBoundary(unittest.TestCase):
    def test_two_dimensional_smiles_only_retains_reaction_atom_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            mol = Chem.MolFromSmiles('C=CC(=O)N[C@H](C)c1ccccc1')
            AllChem.Compute2DCoords(mol)
            with Chem.SDWriter(str(folder/'candidate.sdf')) as writer:
                writer.write(mol)
            record = {'id': 'candidate_000', 'input_file': 'candidate.sdf',
                'smiles': Chem.MolToSmiles(mol), 'beta_index_1based': 1,
                'origins': [{'candidate_id': 'denovo-'+'a'*24+':0:raw'}]}
            row = molecule_row(record, folder, 0)
            reconstructed = Chem.MolFromSmiles(row['standardized_smiles'])
            self.assertEqual(reconstructed.GetNumConformers(), 0)
            self.assertEqual(sum(a.GetAtomMapNum() == 1 for a in reconstructed.GetAtoms()), 1)
            self.assertEqual(row['parent_id'], record['origins'][0]['candidate_id'])
            self.assertNotIn('core_score', row)
            self.assertFalse(any('pose' in key or 'sdf_path' in key for key in row))
            self.assertEqual(table_bytes([row]), table_bytes([dict(reversed(list(row.items())))]))

    def test_generated_three_dimensional_coordinates_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            mol = Chem.AddHs(Chem.MolFromSmiles('C=CC(=O)NC'))
            AllChem.EmbedMolecule(mol, randomSeed=42)
            with Chem.SDWriter(str(folder/'candidate.sdf')) as writer:
                writer.write(mol)
            with self.assertRaisesRegex(ValueError, 'fresh 2D'):
                molecule_row({'input_file': 'candidate.sdf'}, folder, 0)

    def test_control_is_rebuilt_from_primary_sources_and_not_called_redocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()/'control'
            result = prepare_control(REGISTRY/'assets', REGISTRY/'assets/0WM.cif', PRECURSOR_SHA256, folder)
            validate_control(folder, result)
            self.assertEqual(result['state'], 'CONTROL_INPUTS_PREPARED_NOT_REDOCKED')
            self.assertFalse(result['prereaction_pose_reference_available'])
            changed = copy.deepcopy(result)
            changed['sources'][next(iter(changed['sources']))] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'primary-source hashes'):
                validate_control(folder, changed)
            changed = copy.deepcopy(result)
            changed['covalent_control_policy']['max_heavy_atom_rmsd_A'] = 3
            with self.assertRaisesRegex(ValueError, 'criterion'):
                validate_control(folder, changed)
            (folder/'control_atom_map.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'atom mapping'):
                validate_control(folder, result)

    def test_symlink_escape_and_changed_hash_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            (folder/'input').write_text('original')
            checksum = hashlib.sha256(b'original').hexdigest()
            regular(folder, 'input', checksum)
            (folder/'alias').symlink_to(folder/'input')
            with self.assertRaisesRegex(ValueError, 'symlinks'):
                regular(folder, 'alias')
            with self.assertRaisesRegex(ValueError, 'symlinks'):
                inventory(folder)
            with self.assertRaisesRegex(ValueError, 'Noncanonical'):
                regular(folder, '../outside')
            (folder/'input').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                regular(folder, 'input', checksum)

    def test_completed_legacy_generation_cannot_be_adopted_as_native_study(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary).resolve()
            raw = json.dumps({'id': 'historical', 'session_id': 'session', 'user_message_id': 'user'}).encode()
            digest = hashlib.sha256(raw).hexdigest()
            (folder/'request.json').write_bytes(raw)
            (folder/'status.json').write_text(json.dumps({'state': 'COMPLETED'}))
            (folder/'authorization.json').write_text(json.dumps({'request_sha256': digest,
                'session_id': 'session', 'user_message_id': 'user', 'mechanism': 'native_remote_compute'}))
            with self.assertRaisesRegex(ValueError, 'this study'):
                task_binding(folder, digest, 'study-'+'a'*36)

if __name__ == '__main__': unittest.main()
