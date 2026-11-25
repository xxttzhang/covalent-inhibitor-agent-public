"""Verify bounded Epik control states and regenerate independent coordinates."""
from pathlib import Path
import hashlib
from rdkit import Chem
from rdkit.Chem.MolStandardize import rdMolStandardize

from .batch_prereaction_input import fresh, identity


def parent(molecule):
    value = Chem.RemoveHs(Chem.Mol(molecule))
    for atom in value.GetAtoms():
        atom.SetAtomMapNum(0)
    return identity(rdMolStandardize.Uncharger().uncharge(value))


def export(source, reference_smiles, output):
    source, output = Path(source), Path(output)
    if source.is_symlink() or not source.is_file():
        raise ValueError('Control LigPrep requires its registered regular output')
    original = Chem.MolFromSmiles(reference_smiles)
    if original is None:
        raise ValueError('Invalid reference precursor')
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    supplier = Chem.MaeMolSupplier(str(source), sanitize=True, removeHs=False)
    states = []
    while not supplier.atEnd():
        value = next(supplier)
        if value is None or len(states) >= 2:
            raise ValueError('Control preparation must produce one or two readable states')
        if parent(value) != parent(original):
            raise ValueError('Control state changed parent graph, tautomer or stereochemistry')
        states.append(value)
    if not states or len({identity(m) for m in states}) != len(states):
        raise ValueError('Control state count is empty or duplicated')
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for index, molecule in enumerate(states):
        name = f'CONTROL_AFATINIB_STATE{index}'
        prepared, record = fresh({'candidate_id': name, 'parent_id': 'CONTROL_AFATINIB',
            'mapped_smiles': identity(molecule)}, control=True)
        path = output / (name + '.sdf')
        with Chem.SDWriter(str(path)) as writer:
            writer.write(prepared)
        loaded = list(Chem.SDMolSupplier(str(path), removeHs=False))
        if len(loaded) != 1 or loaded[0] is None or identity(loaded[0]) != identity(molecule):
            raise ValueError('Control state serialization changed identity')
        records.append({**record, 'role': 'reference_control', 'file': path.name,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'state': 'PREPARED_NOT_DOCKED'})
    if hashlib.sha256(source.read_bytes()).hexdigest() != checksum:
        raise ValueError('Control LigPrep output changed during validation')
    return {'schema': 'denovo-control-states-v1', 'source_sha256': checksum, 'states': records,
        'parent_identity_verified': True, 'docking_performed': False}
