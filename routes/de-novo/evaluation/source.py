"""Freeze one native-study candidate for independent refinement, without compute.

The installed verifier replays the generation outputs. Task-owned Python is never
imported. The original evidence is retained for replay after transport; only fresh
2D/SMILES inputs are passed to the independent docking engine.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile

WORKSPACE = Path(__file__).resolve().parents[3]
SHARED = WORKSPACE / 'science'
sys.path.insert(0, str(SHARED/'src'))

from rdkit import Chem
import numpy as np
from prepare import REGISTRY, prepare, verify
from control import prepare_control

VERSION = 'native-denovo-independent-source-v1'
PRECURSOR_SHA256 = 'a063c061aeae94d6d9f99a3d959455ccb3a595ccd9559260605ce67449b74fb1'
MAX_BYTES = 256 * 1024 * 1024


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()


def regular(root, name, digest=None):
    root = Path(root).resolve(strict=True)
    if not isinstance(name, str) or not name or Path(name).is_absolute() or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('Noncanonical source path')
    path = root / name
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError('Source must be a regular file without symlinks')
    path.resolve(strict=True).relative_to(root)
    if digest is not None and (not re.fullmatch('[a-f0-9]{64}', digest) or sha(path) != digest):
        raise ValueError('Source checksum changed: ' + name)
    return path


def inventory(root):
    root = Path(root).resolve(strict=True)
    files = {}
    total = 0
    for item in sorted(root.rglob('*')):
        if item.is_symlink():
            raise ValueError('Source inventory cannot contain symlinks')
        if item.is_dir():
            continue
        if not item.is_file():
            raise ValueError('Source inventory contains a nonregular file')
        total += item.stat().st_size
        if total > MAX_BYTES or len(files) >= 10000:
            raise ValueError('Source exceeds bounded transport inventory')
        files[item.relative_to(root).as_posix()] = sha(item)
    return files


def molecule_row(record, folder, index, *, control=False):
    mol = Chem.MolFromMolFile(str(folder / record['input_file']), removeHs=True)
    if mol is None or mol.GetNumConformers() != 1 or mol.GetConformer().Is3D():
        raise ValueError('Independent ligand input must be fresh 2D')
    if Chem.MolToSmiles(mol, isomericSmiles=True) != record['smiles']:
        raise ValueError('Independent chemical identity changed')
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(atom.GetIdx() + 1)
    beta = record['beta_index_1based']
    return {'input_sdf_index': index, 'compound_id': record['id'],
        'standardized_smiles': Chem.MolToSmiles(mol, isomericSmiles=True),
        'route': 'de-novo', 'target_id': 'EGFR', 'site_id': 'EGFR_C797',
        'parent_id': 'PDB:4G5J:0WN' if control else record['origins'][0]['candidate_id'],
        'warhead_class': 'acrylamide', 'electrophilic_atom': 'map:' + str(beta),
        'electrophilic_atom_map': beta, 'nucleophilic_atom': 'A:CYS:797:SG',
        'covdock_reaction_type': 'Michael Addition',
        'independent_role': 'redocking_control' if control else 'candidate'}


def table_bytes(rows):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=sorted(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def validate_control(folder, control):
    """Replay primary CCD/crystal reconstruction, not a caller's control label."""
    if set(control['sources'].values()) != {sha(REGISTRY / 'assets' / name) for name in ('0WN.cif', '0WM.cif', '4G5J.pdb')}:
        raise ValueError('Control primary-source hashes changed')
    with tempfile.TemporaryDirectory(prefix='denovo-control-replay-') as temporary:
        rebuilt = Path(temporary)/'control'
        expected = prepare_control(REGISTRY/'assets', REGISTRY/'assets/0WM.cif', PRECURSOR_SHA256, rebuilt)
        for key in ('state', 'record', 'covalent_control_policy', 'prereaction_pose_reference_available'):
            if control.get(key) != expected[key]:
                raise ValueError('Control identity or preregistered criterion changed')
        for name in ('control_afatinib.sdf', 'control_crystal_adduct.sdf'):
            observed = Chem.MolFromMolFile(str(folder/name), removeHs=True)
            original = Chem.MolFromMolFile(str(rebuilt/name), removeHs=True)
            if (observed is None or Chem.MolToSmiles(observed) != Chem.MolToSmiles(original)
                    or observed.GetNumAtoms() != original.GetNumAtoms()
                    or not np.allclose(observed.GetConformer().GetPositions(), original.GetConformer().GetPositions(), atol=1e-4, rtol=0)):
                raise ValueError('Control chemical graph or crystal reference changed')
        if json.loads((folder/'control_atom_map.json').read_text()) != json.loads((rebuilt/'control_atom_map.json').read_text()):
            raise ValueError('Control atom mapping changed')


def task_binding(task, expected, study):
    request = json.loads(regular(task, 'request.json', expected).read_text())
    auth = json.loads(regular(task, 'authorization.json').read_text())
    status = json.loads(regular(task, 'status.json').read_text())
    if (request.get('study_id') != study or status.get('state') != 'COMPLETED'
            or auth.get('request_sha256') != expected or auth.get('mechanism') != 'native_remote_compute'
            or auth.get('session_id') != request.get('session_id')
            or auth.get('user_message_id') != request.get('user_message_id')):
        raise ValueError('Source is not a completed native generation in this study')
    basis = request.get('scientific_basis') or {}
    hypothesis = json.loads(basis.get('hypothesis_json', '{}'))
    if hypothesis.get('hypothesis', {}).get('study_id') != study:
        raise ValueError('Generation scientific hypothesis belongs to another study')
    return request


def freeze(task, expected, study, project, candidate_id, output):
    task = Path(task).resolve(strict=True)
    if not re.fullmatch(r'study-[a-f0-9-]{36}', study) or not project:
        raise ValueError('A native study/project binding is required')
    request = task_binding(task, expected, study)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('Independent source output must be new')
    output.mkdir(parents=True)
    # No generation task script is copied or executed. This is an evidence
    # snapshot; approval remains the original native receipt, not a new grant.
    frozen = output / 'generation'
    frozen.mkdir()
    for name in ('request.json', 'authorization.json', 'status.json'):
        shutil.copyfile(regular(task, name), frozen / name)
    for name in ('download', 'acceptance'):
        contents = inventory(task / name)
        for relative, digest in contents.items():
            dest = frozen / name / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(regular(task / name, relative, digest), dest)
    inputs = prepare(frozen, expected, output / 'independent', candidate_id)
    if len(inputs['records']) != 1 or len(inputs['records'][0]['origins']) != 1:
        raise ValueError('This MVP freezes exactly one specified accepted representation')
    control = prepare_control(REGISTRY / 'assets', REGISTRY / 'assets/0WM.cif', PRECURSOR_SHA256, output / 'control')
    rows = [molecule_row(inputs['records'][0], output/'independent', 0),
            molecule_row(control['record'], output/'control', 1, control=True)]
    (output / 'candidates.csv').write_bytes(table_bytes(rows))
    pairs = [[row['input_sdf_index'], row['compound_id']] for row in rows]
    identity = hashlib.sha256(json.dumps(pairs, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    value = {'schema': VERSION, 'route': 'de-novo', 'study_id': study, 'project_id': project,
        'task_id': request['id'], 'request_sha256': expected,
        'hypothesis_sha256': request['scientific_basis']['hypothesis_sha256'],
        'candidate_id': candidate_id, 'source_candidate_sha256': inputs['records'][0]['origins'][0]['source_sha256'],
        'receptor_sha256': sha(output/'independent/protein_A.pdb'),
        'input_contract': {'input_count': 2, 'input_sha256': sha(output/'candidates.csv'), 'identity_sha256': identity},
        'generated_coordinates_used_for_refinement': False,
        'candidate_count': 1, 'control_count': 1,
        'independent_prereaction_evaluation': 'NOT_PERFORMED',
        'independent_adduct_evaluation': 'NOT_PERFORMED', 'control_redocking': 'NOT_PERFORMED',
        'ready_for_scientific_handoff': False, 'compute_submitted': False,
        'files': inventory(output)}
    (output/'source.json').write_bytes(encoded(value))
    validate(output, sha(output/'source.json'))
    return {'source_id': 'denovo-review-' + sha(output/'source.json')[:32],
        'source_manifest_sha256': sha(output/'source.json'), 'source': value}


def validate(folder, expected):
    folder = Path(folder).resolve(strict=True)
    value = json.loads(regular(folder, 'source.json', expected).read_text())
    if (value.get('schema') != VERSION or value.get('route') != 'de-novo'
            or value.get('candidate_count') != 1 or value.get('control_count') != 1
            or any(value.get(key) is not False for key in ('generated_coordinates_used_for_refinement',
                'ready_for_scientific_handoff', 'compute_submitted'))
            or any(value.get(key) != 'NOT_PERFORMED' for key in ('independent_prereaction_evaluation',
                'independent_adduct_evaluation', 'control_redocking'))):
        raise ValueError('Independent source scope changed')
    actual = inventory(folder)
    actual.pop('source.json')
    if actual != value['files']:
        raise ValueError('Independent source inventory changed')
    request = task_binding(folder/'generation', value['request_sha256'], value['study_id'])
    if request['id'] != value['task_id'] or request['scientific_basis']['hypothesis_sha256'] != value['hypothesis_sha256']:
        raise ValueError('Source native study identity changed')
    checked = verify(folder/'generation', value['request_sha256'])
    origins = [c for c in checked['candidates'] if c['candidate_id'] == value['candidate_id']]
    if len(origins) != 1 or not origins[0]['structural_qc_passed'] or origins[0]['source_sha256'] != value['source_candidate_sha256']:
        raise ValueError('Selected candidate failed fresh registered structural/source validation')
    prepared = json.loads(regular(folder, 'independent/input_manifest.json').read_text())
    if (prepared['request_sha256'] != value['request_sha256'] or len(prepared['records']) != 1
            or prepared['records'][0]['origins'] != origins or prepared['records'][0]['smiles'] != origins[0]['smiles']
            or prepared['scientific_basis'] != checked['scientific_basis']
            or sha(folder/'independent/protein_A.pdb') != value['receptor_sha256']
            or value['receptor_sha256'] != sha(REGISTRY/'assets/protein_A.pdb')):
        raise ValueError('Prepared inputs differ from native generation or registered receptor')
    control = json.loads(regular(folder, 'control/control_manifest.json').read_text())
    # Source coordinates stay exclusively in the crystal reference. This is not
    # a completed control and cannot satisfy the independent validation gate.
    if control['state'] != 'CONTROL_INPUTS_PREPARED_NOT_REDOCKED' or control['record']['id'] != 'control_afatinib':
        raise ValueError('Redocking control identity changed')
    validate_control(folder/'control', control)
    rows = [molecule_row(prepared['records'][0], folder/'independent', 0),
            molecule_row(control['record'], folder/'control', 1, control=True)]
    if (folder/'candidates.csv').read_bytes() != table_bytes(rows):
        raise ValueError('Independent candidate table differs from source-bound 2D molecules')
    from covscreen.refinement_input import verify_refinement_input
    contract = value['input_contract']
    verify_refinement_input(folder/'candidates.csv', contract['input_count'], contract['input_sha256'],
                            contract['identity_sha256'], expected_route='de-novo')
    return value


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('task', type=Path)
    parser.add_argument('--request-sha256', required=True)
    parser.add_argument('--study-id', required=True)
    parser.add_argument('--project-id', required=True)
    parser.add_argument('--candidate-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(freeze(args.task, args.request_sha256, args.study_id, args.project_id,
                            args.candidate_id, args.output), ensure_ascii=False))
