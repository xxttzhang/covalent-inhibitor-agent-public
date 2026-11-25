import sys
from pathlib import Path
import unittest
from rdkit import Chem
from rdkit.Chem import AllChem
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'evaluation'))
from prepare import independent_input

class IndependentInputs(unittest.TestCase):
    def test_removes_anchor_coordinates_preserves_stereochemistry_and_graph(self):
        source = Chem.AddHs(Chem.MolFromSmiles('C=CC(=O)N[C@H](C)c1ccccc1'))
        self.assertEqual(AllChem.EmbedMolecule(source, randomSeed=17), 0)
        result = independent_input(source)
        self.assertTrue(source.GetConformer().Is3D())
        self.assertFalse(result.GetConformer().Is3D())
        self.assertEqual(Chem.MolToSmiles(result), Chem.MolToSmiles(source))
        self.assertEqual([(b.GetBeginAtomIdx(), b.GetEndAtomIdx(), b.GetBondType()) for b in result.GetBonds()],
                         [(b.GetBeginAtomIdx(), b.GetEndAtomIdx(), b.GetBondType()) for b in source.GetBonds()])
    def test_invalid_structure_rejected(self):
        with self.assertRaises(ValueError): independent_input(None)

if __name__ == '__main__': unittest.main()
