"""Score-selected, receptor-aligned control RMSD; never a ligand fit or MD grant."""
import json
import math
from pathlib import Path
import re

from covscreen import covdock_readback
from covscreen.jobs import sha256_file
from covscreen.layout import science_script
from covscreen.pose_consistency import protein_residue_identity
from covscreen.refinement_output import Files, SPECS, _covdock_sdk

VERSION = 'denovo-control-redocking-v1'
MAX_MAPPINGS = 4096
POLICY = {'pose_selection': 'best vendor score before RMSD calculation',
    'max_heavy_atom_rmsd_A': 2.0,
    'alignment': 'receptor backbone, symmetry-aware ligand mapping; no ligand fitting'}


def reference_pdb():
    return Path(__file__).resolve().parents[2]/'registry/egfr_c797_v1/assets/4G5J.pdb'


def implementation():
    import numpy as np
    from rdkit import rdBase
    from covscreen import pose_consistency
    paths = [Path(__file__), Path(pose_consistency.__file__), reference_pdb(),
        *[science_script(name) for name in ('read_covdock_control.py',
            'extract_covdock_pose_coordinates.py', 'inspect_covdock_output.py')]]
    return {'files': {str(p.resolve(strict=True)): sha256_file(p) for p in paths},
            'rdkit': rdBase.rdkitVersion, 'numpy': np.__version__}


def freeze():
    return {'version': VERSION, 'policy': POLICY.copy(), 'max_mappings': MAX_MAPPINGS,
            'implementation': implementation()}


def validate(binding):
    if binding != freeze():
        raise ValueError('Control evaluator or policy changed after plan freeze')


def select_pose(rows, score_property, *, compound_id='control_afatinib', input_index=1):
    """The entire already-rechecked container audit determines the choice."""
    if (compound_id, input_index) not in {('control_afatinib', 1), ('candidate_000', 0)}:
        raise ValueError('Unsupported independent pose role')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise ValueError('Control requires a complete bounded pose census')
    choices = []
    for index, row in enumerate(rows):
        if type(row.get('pose_index')) is not int or row['pose_index'] != index:
            raise ValueError('Control pose census is incomplete or ambiguous')
        title = row.get('title', '')
        if not isinstance(title, str):
            raise ValueError('Invalid pose title')
        identity = re.fullmatch(re.escape(compound_id)+r'__p[A-Za-z0-9._-]+__i'+str(input_index), title)
        if not identity:
            # A malformed label must not silently hide a competing control pose.
            if title.startswith(compound_id) or re.search(r'__i'+str(input_index)+r'(?:\D|$)', title):
                raise ValueError('Ambiguous control pose identity')
            continue
        score = row.get(score_property)
        if type(score) not in (int, float) or not math.isfinite(score):
            raise ValueError('Control pose lacks a finite vendor score')
        choices.append(row)
    if not choices:
        raise ValueError('No control pose in verified container')
    # Fixed deterministic tie-break independent of ligand coordinates.
    return min(choices, key=lambda row: (row[score_property], row['pose_index']))


def protein_reference(path):
    rows = []
    for line in Path(path).read_text().splitlines():
        if line.startswith('MODEL'):
            raise ValueError('Control reference must be a single crystal model')
        if (line.startswith('ATOM  ') and line[21:22] == 'A'
                and line[12:16].strip() == 'CA' and line[16:17] in (' ', 'A')):
            rows.append({'chain': 'A', 'resnum': int(line[22:26]), 'icode': line[26:27].strip(),
                'resname': line[17:20].strip(), 'atom_name': 'CA',
                'xyz': [float(line[30:38]), float(line[38:46]), float(line[46:54])]})
    return rows


def receptor_transform(reference, moving):
    import numpy as np
    def indexed(rows):
        result = {}
        for row in rows:
            key = (row['chain'], row['resnum'], row['icode'])
            if (row['atom_name'] != 'CA' or row['chain'] != 'A'
                    or type(row['resnum']) is not int or key in result):
                raise ValueError('Ambiguous receptor CA identity')
            result[key] = row
        return result
    fixed, mobile = indexed(reference), indexed(moving)
    if len(fixed) < 3 or not set(fixed).issubset(mobile):
        raise ValueError('Control receptor lacks reference CA residues')
    keys = sorted(fixed)
    if any(protein_residue_identity(fixed[k]['resname']) !=
           protein_residue_identity(mobile[k]['resname']) for k in keys):
        raise ValueError('Control receptor sequence identity changed')
    a, b = (np.asarray([rows[k]['xyz'] for k in keys], dtype=float) for rows in (fixed, mobile))
    if a.shape != (len(keys), 3) or b.shape != a.shape or not np.isfinite([a, b]).all():
        raise ValueError('Invalid receptor coordinates')
    ac, bc = a - a.mean(axis=0), b - b.mean(axis=0)
    if min(np.linalg.matrix_rank(ac), np.linalg.matrix_rank(bc)) < 2:
        raise ValueError('Degenerate receptor alignment')
    u, _, vt = np.linalg.svd(bc.T @ ac)
    correction = np.eye(3)
    correction[2, 2] = 1 if np.linalg.det(u @ vt) > 0 else -1
    rotation = u @ correction @ vt
    translation = a.mean(axis=0) - b.mean(axis=0) @ rotation
    rmsd = float(np.sqrt(np.mean(np.sum((b @ rotation + translation - a)**2, axis=1))))
    return rotation, translation, rmsd, len(keys), len(mobile)-len(keys)


def ligand_graph(pose):
    import numpy as np
    from rdkit import Chem
    atoms = pose['ligand_heavy_atoms']
    if not 1 <= len(atoms) <= 256:
        raise ValueError('Control ligand exceeds atom bounds')
    rw, names, indices = Chem.RWMol(), {}, set()
    for row in atoms:
        name, index = row['atom_name'], row['atom_index']
        if (not name or name in names or type(index) is not int or index < 1 or index in indices
                or type(row['atomic_number']) is not int or row['atomic_number'] <= 1
                or type(row['formal_charge']) is not int or type(row['isotope']) is not int):
            raise ValueError('Ambiguous control ligand atom identity')
        atom = Chem.Atom(row['atomic_number'])
        if atom.GetSymbol().upper() != row['element'].upper():
            raise ValueError('Control ligand element differs')
        atom.SetFormalCharge(row['formal_charge']); atom.SetIsotope(row['isotope'])
        names[name] = rw.AddAtom(atom); indices.add(index)
    types = {1: Chem.BondType.SINGLE, 2: Chem.BondType.DOUBLE, 3: Chem.BondType.TRIPLE,
             1.5: Chem.BondType.AROMATIC}
    for bond in pose['ligand_heavy_bonds']:
        first, second = names[bond['atom_name_1']], names[bond['atom_name_2']]
        if first == second or rw.GetBondBetweenAtoms(first, second) or bond['order'] not in types:
            raise ValueError('Ambiguous control ligand bond')
        rw.AddBond(first, second, types[bond['order']])
    mol = rw.GetMol()
    Chem.SanitizeMol(mol)
    xyz = np.asarray([row['xyz'] for row in atoms], dtype=float)
    if xyz.shape != (len(atoms), 3) or not np.isfinite(xyz).all():
        raise ValueError('Invalid control ligand coordinates')
    conf = Chem.Conformer(len(atoms)); conf.Set3D(True)
    for index, point in enumerate(xyz): conf.SetAtomPosition(index, point)
    mol.AddConformer(conf)
    Chem.AssignStereochemistryFrom3D(mol)
    return mol, names


def measure(reference, reference_ca, reference_partner, pose, *, max_mappings=MAX_MAPPINGS):
    import numpy as np
    from rdkit import Chem
    if type(max_mappings) is not int or not 1 <= max_mappings <= MAX_MAPPINGS:
        raise ValueError('Invalid mapping bound')
    moving, names = ligand_graph(pose)
    if (reference is None or reference.GetNumConformers() != 1
            or not reference.GetConformer().Is3D()
            or reference.GetNumAtoms() != reference.GetNumHeavyAtoms()
            or Chem.MolToSmiles(reference, isomericSmiles=True) != Chem.MolToSmiles(moving, isomericSmiles=True)):
        raise ValueError('Control capped ligand graph or stereochemistry differs from crystal reference')
    partner = names[pose['covalent_partner_atom_name']]
    matches = moving.GetSubstructMatches(reference, uniquify=False, useChirality=True,
                                        maxMatches=max_mappings+1)
    if len(matches) > max_mappings:
        raise ValueError('Control symmetry enumeration exceeds bound; no truncated RMSD accepted')
    matches = [match for match in matches if match[reference_partner] == partner]
    if not matches:
        raise ValueError('Control attachment atom has no complete graph mapping')
    rotation, translation, protein_rmsd, count, extra = receptor_transform(reference_ca, pose['protein_ca'])
    xyz = np.asarray(moving.GetConformer().GetPositions()) @ rotation + translation
    fixed = np.asarray(reference.GetConformer().GetPositions())
    if not np.isfinite(fixed).all(): raise ValueError('Invalid reference ligand coordinates')
    scores = [(float(np.sqrt(np.mean(np.sum((xyz[list(match)]-fixed)**2, axis=1)))), match)
              for match in matches]
    rmsd, mapping = min(scores)
    return {'state': 'PASS' if rmsd <= POLICY['max_heavy_atom_rmsd_A'] else 'FAIL',
        'heavy_atom_rmsd_A': rmsd, 'receptor_ca_rmsd_A': protein_rmsd,
        'reference_ca_count': count, 'additional_prepared_ca_count': extra,
        'ligand_heavy_atom_count': len(fixed), 'symmetry_mappings_evaluated': len(matches),
        'reference_to_pose_atom_indices': [pose['ligand_heavy_atoms'][i]['atom_index'] for i in mapping],
        'ligand_fitting': False, 'policy': POLICY.copy(), 'md_ready': False}


def read_pose(root, manager, spec, job, review, *, input_index, compound_id, version):
    """Read one score-selected role after the caller replays all vendor evidence."""
    from covscreen.md_preparation_evidence import registered_output
    validate(spec.parameters.get('denovo_control_readback'))
    binding = covdock_readback.validate(spec.parameters['covdock_readback'], 'A:797')
    folder = manager.registry.job_dir(spec.run_id)/'artifacts'
    files, hashes = Files(folder), review['output_sha256']
    for name, digest in hashes.items(): registered_output(root, folder, spec, job, name, digest)
    manifest = files.json(SPECS['covdock'][0], hashes[SPECS['covdock'][0]])
    import pandas as pd
    import io
    rows = pd.read_parquet(io.BytesIO(files.read(SPECS['covdock'][1], hashes[SPECS['covdock'][1]], parse=True)))
    selected = [r for r in rows.to_dict(orient='records') if r['input_sdf_index'] == input_index]
    if len(selected) != 1 or selected[0]['compound_id'] != compound_id:
        raise ValueError('Registered pose table lost the exact candidate/control identity')
    row = selected[0]
    audit = files.json(row['covdock_inspection_file'], row['covdock_inspection_sha256'])
    chosen = select_pose(audit['rows'], manifest['score_property'], compound_id=compound_id, input_index=input_index)
    if chosen['pose_index'] != row['covdock_pose_index'] or chosen['title'] != row['covdock_title']:
        raise ValueError('Registered control row is not the independently score-selected pose')
    path, name = files.path(row['covdock_pose_file'])
    files.read(name, hashes[name])
    request = {'version': version, 'files': [str(path)],
        'attachment_residue': 'A:797', 'pose_index': chosen['pose_index'], 'title': chosen['title'],
        'score_property': manifest['score_property'], 'score': chosen[manifest['score_property']]}
    actual = _covdock_sdk(request, binding['runtime'], science_script('read_covdock_control.py'))
    if (actual.get('version') != request['version'] or actual.get('input') != str(path)
            or actual.get('scientific_quality_verified') is not False
            or actual.get('pose_count') != len(audit['rows'])
            or actual['pose']['pose_index'] != chosen['pose_index'] or actual['pose']['title'] != chosen['title']):
        raise ValueError('Control coordinate response differs from selected pose')
    for name, digest in hashes.items(): registered_output(root, folder, spec, job, name, digest)
    validate(spec.parameters['denovo_control_readback'])
    covdock_readback.validate(binding, 'A:797')
    return actual['pose'], {'run_id': spec.run_id,
        'pose_index': chosen['pose_index'], 'title': chosen['title'],
        'score_property': request['score_property'], 'vendor_score': request['score'],
        'pose_container_sha256': hashes[files.path(str(path))[1]]}


def revalidate_outputs(root, manager, spec, job, review):
    from covscreen.md_preparation_evidence import registered_output
    folder = manager.registry.job_dir(spec.run_id)/'artifacts'
    for name, digest in review['output_sha256'].items():
        registered_output(root, folder, spec, job, name, digest)
    validate(spec.parameters['denovo_control_readback'])
    covdock_readback.validate(spec.parameters['covdock_readback'], 'A:797')


def evaluate(root, source_folder, manager, spec, job, review):
    """Called only after native approval and complete vendor-evidence replay."""
    from rdkit import Chem
    controls = [row for row in review['candidates'] if row['input_sdf_index'] == 1]
    if len(controls) != 1 or controls[0]['compound_id'] != 'control_afatinib':
        raise ValueError('Review lost its exact control identity')
    if controls[0]['engine_status'] != 'SUCCESS':
        return {'version': VERSION, 'state': 'ENGINE_FAILED', 'md_ready': False}
    pose, provenance = read_pose(root, manager, spec, job, review, input_index=1,
        compound_id='control_afatinib', version='covdock-control-coordinates-v1')
    control = source_folder/'control'
    control_manifest = json.loads((control/'control_manifest.json').read_text())
    if control_manifest['covalent_control_policy'] != POLICY:
        raise ValueError('Control input policy differs from frozen evaluator')
    reference = Chem.MolFromMolFile(str(control/'control_crystal_adduct.sdf'), removeHs=True)
    atom_map = json.loads((control/'control_atom_map.json').read_text())
    measured = measure(reference, protein_reference(reference_pdb()), atom_map['C30'], pose)
    geometry_state = measured['state']
    physical = controls[0]['gate']
    if physical['physical_validity'] == 'FAIL':
        measured['state'] = 'FAIL'
    elif physical['physical_validity'] != 'PASS' or physical['physical_evidence_complete'] is not True:
        measured['state'] = 'EVIDENCE_INSUFFICIENT'
    revalidate_outputs(root, manager, spec, job, review)
    return {'version': VERSION, **measured, **provenance,
        'rmsd_validation': geometry_state, 'physical_gate': physical,
        'control_reference_sha256': sha256_file(control/'control_crystal_adduct.sdf'),
        'scope': 'covalent_control_pose_recovery_only', 'scientific_result_verified': False}
