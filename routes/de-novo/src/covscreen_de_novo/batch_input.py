"""Freeze mapped, coordinate-independent batch input for native preparation."""
import hashlib
import json
import argparse
import sys
from pathlib import Path

from rdkit import Chem
from rdkit.Chem import rdDepictor

from .batch_selection import preparation_records


def validate(folder, expected_manifest_sha256):
    folder = Path(folder)
    def read(name):
        filename = folder / name
        if any(p.is_symlink() for p in (filename, *filename.parents)) or not filename.is_file():
            raise ValueError('Batch input must be regular data without symlinks')
        return filename.read_bytes()
    raw = read('input.json')
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
        raise ValueError('Batch input manifest changed')
    manifest = json.loads(raw)
    if manifest.get('schema') != 'pocketxmol-batch-input-v1':
        raise ValueError('Unsupported batch input schema')
    binding = json.loads(read('binding.json'))
    if binding['manifest_sha256'] != expected_manifest_sha256 or binding['attachment_residue'] != 'A:797':
        raise ValueError('Batch binding differs from the requested input')
    if hashlib.sha256(read('receptor.pdb')).hexdigest() != binding['receptor_sha256']:
        raise ValueError('Batch receptor changed')
    sdf = read('ligands.sdf')
    if hashlib.sha256(sdf).hexdigest() != manifest['ligands_sha256']:
        raise ValueError('Batch ligand file changed')
    import io
    molecules = list(Chem.ForwardSDMolSupplier(io.BytesIO(sdf), removeHs=False))
    rows = manifest['candidates']
    if not 1 <= len(rows) <= 200 or len(molecules) != len(rows) or len(rows) != manifest['candidate_count']:
        raise ValueError('Batch candidate denominator differs')
    if len({row['candidate_id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate batch candidate identity')
    for index, (mol, row) in enumerate(zip(molecules, rows)):
        if (mol is None or row['input_index'] != index
                or not mol.HasProp('INPUT_SDF_INDEX')
                or mol.GetIntProp('INPUT_SDF_INDEX') != index
                or mol.GetProp('_Name') != row['candidate_id']
                or mol.GetProp('PARENT_CANDIDATE_ID') != row['parent_id']
                or mol.GetProp('SOURCE_SHA256') != row['source_sha256']
                or Chem.MolToSmiles(mol, isomericSmiles=True) != row['mapped_smiles']
                or mol.GetConformer().Is3D()):
            raise ValueError('Batch ligand identity or independent coordinates differ')
        maps = {atom.GetAtomMapNum(): atom.GetIdx() for atom in mol.GetAtoms()}
        bond = mol.GetBondBetweenAtoms(maps[905], maps[906])
        if bond is None or bond.GetBondType() != Chem.BondType.DOUBLE:
            raise ValueError('Batch reaction mapping differs')
    return manifest, binding


def export(selection, output):
    records = preparation_records(selection)
    if not records:
        raise ValueError('No chemically eligible candidates remain')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    filename = output / 'ligands.sdf'
    identities = []
    with Chem.SDWriter(str(filename)) as writer:
        for index, row in enumerate(records):
            mol = Chem.MolFromSmiles(row['mapped_smiles'])
            if mol is None:
                raise ValueError('Invalid mapped preparation molecule')
            name = 'PXM_' + hashlib.sha256(row['parent_id'].encode()).hexdigest()[:24]
            mol.SetProp('_Name', name)
            mol.SetIntProp('INPUT_SDF_INDEX', index)
            mol.SetProp('PARENT_CANDIDATE_ID', row['parent_id'])
            mol.SetProp('SOURCE_SHA256', row['source_sha256'])
            rdDepictor.Compute2DCoords(mol)
            writer.write(mol)
            identities.append({**row, 'candidate_id': name, 'input_index': index})
    parsed = list(Chem.SDMolSupplier(str(filename), removeHs=False))
    if len(parsed) != len(records) or any(mol is None for mol in parsed):
        raise ValueError('Prepared batch SDF readback failed')
    for mol, row in zip(parsed, identities):
        if (mol.GetProp('_Name') != row['candidate_id']
                or Chem.MolToSmiles(mol, isomericSmiles=True) != row['mapped_smiles']
                or mol.GetConformer().Is3D()):
            raise ValueError('Prepared batch identity changed in SDF serialization')
    manifest = {
        'schema': 'pocketxmol-batch-input-v1', 'task_id': selection['task_id'],
        'generation_provenance': selection['provenance'],
        'selection_sha256': hashlib.sha256(json.dumps(selection, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
        'ligands_sha256': hashlib.sha256(filename.read_bytes()).hexdigest(),
        'candidate_count': len(records), 'candidates': identities,
        'generated_coordinates_reused': False, 'compute_submitted': False,
    }
    with (output / 'input.json').open('x') as stream:
        json.dump(manifest, stream, indent=2, allow_nan=False)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(json.load(sys.stdin), args.output)))
