"""Tiny software-only receptor fragment, never a scientifically prepared protein."""
from rdkit import Chem


def receptor_molecule():
    molecule = Chem.RWMol()
    for index, (symbol, name) in enumerate([('C', ' CB '), ('S', ' SG '), ('H', ' HG ')], 1):
        atom = Chem.Atom(symbol)
        atom.SetNoImplicit(True)
        info = Chem.AtomPDBResidueInfo(atomName=name, serialNumber=index, residueName='CYS',
            residueNumber=797, chainId='A', occupancy=1.0)
        atom.SetMonomerInfo(info)
        molecule.AddAtom(atom)
    molecule.AddBond(0, 1, Chem.BondType.SINGLE)
    molecule.AddBond(1, 2, Chem.BondType.SINGLE)
    conformer = Chem.Conformer(3)
    for index, x in enumerate([0.0, 1.8, 3.0]):
        conformer.SetAtomPosition(index, (x, 0.0, 0.0))
    molecule.AddConformer(conformer)
    result = molecule.GetMol()
    result.UpdatePropertyCache(strict=False)
    return result


def write_receptor(path, molecule=None):
    molecule = receptor_molecule() if molecule is None else molecule
    if path.suffix == '.pdb':
        path.write_text(Chem.MolToPDBBlock(molecule))
    else:
        with Chem.MaeWriter(str(path)) as writer:
            writer.write(molecule)
    return path
