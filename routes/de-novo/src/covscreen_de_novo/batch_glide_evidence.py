"""Read registered original Glide subjobs and independently evaluate each pose."""
import gzip
import io
import json
from pathlib import Path
from rdkit import Chem
from .batch_prereaction_geometry import measure, summarize, POLICY


def read(root, manager, spec, result, inputs, source):
    from covscreen.md_preparation_evidence import registered_output
    from covscreen.jobs import sha256_file
    from .batch_prereaction_input import identity
    from .batch_control_states import parent
    if (result.state != 'COMPLETED' or result.return_code != 0
            or any(getattr(result, key) != getattr(spec, key) for key in
                ('run_id', 'project_id', 'route', 'engine', 'attempt', 'parent_run_id'))):
        raise ValueError('Glide evaluation requires its completed registered task')
    root = Path(root)
    output = manager.registry.job_dir(spec.run_id) / 'artifacts'
    hashes = {}
    def registered(name, digest=None):
        path = registered_output(root, output, spec, result, name, digest)
        hashes[str(path.relative_to(root))] = sha256_file(path)
        return path
    receipt = json.loads(registered('prereaction/execution.json').read_text())
    if (receipt['schema'] != 'denovo-batch-glide-execution-v1' or receipt['run_id'] != spec.run_id
            or receipt['source_manifest_sha256'] != spec.parameters['prereaction_manifest_sha256']):
        raise ValueError('Glide receipt differs from approved inputs')
    registered('prereaction/prereaction_grid.zip', receipt['grid_sha256'])
    controls = json.loads(registered('prereaction/control-states.json').read_text())
    registered('prereaction/control_states.mae', controls['source_sha256'])
    candidates = [row for row in inputs['states'] if row['state'] == 'PREPARED_NOT_DOCKED']
    expected = [*candidates, *controls['states']]
    if (receipt['states'] != expected or not 1 <= len(controls['states']) <= 2
            or len({r['candidate_id'] for r in expected}) != len(expected)):
        raise ValueError('Glide output candidate/control denominator changed')
    originals = {}
    for row in expected:
        control = row.get('role') == 'reference_control'
        path = registered('prereaction/controls/' + row['file'], row['sha256']) if control else source / row['file']
        if sha256_file(path) != row['sha256']:
            raise ValueError('Glide source molecular input changed')
        molecules = list(Chem.SDMolSupplier(str(path), removeHs=False))
        if len(molecules) != 1 or molecules[0] is None or identity(molecules[0]) != row['canonical_smiles']:
            raise ValueError('Glide source molecule identity changed')
        if control and parent(molecules[0]) != parent(Chem.MolFromSmiles(inputs['reference']['canonical_smiles'])):
            raise ValueError('Glide control parent differs from official precursor')
        originals[row['candidate_id']] = molecules[0]
    receptor = root / inputs['source']['receptor']
    if sha256_file(receptor) != inputs['source']['receptor_sha256']:
        raise ValueError('Glide unreacted receptor changed')
    protein = next(Chem.MaeMolSupplier(str(receptor), sanitize=False, removeHs=False))
    targets = [atom.GetIdx() for atom in protein.GetAtoms() if atom.GetPDBResidueInfo()
        and atom.GetPDBResidueInfo().GetChainId().strip() == 'A'
        and atom.GetPDBResidueInfo().GetResidueNumber() == 797
        and atom.GetPDBResidueInfo().GetResidueName().strip() == 'CYS'
        and atom.GetPDBResidueInfo().GetName().strip() == 'SG']
    if len(targets) != 1:
        raise ValueError('Glide receptor target sulfur is ambiguous')
    sulfur = protein.GetConformer().GetPositions()[targets[0]]
    by_name = {row['candidate_id']: row for row in expected}
    rows, seen = [], set()
    for raw in receipt['raw_outputs']:
        name = Path(raw['path']).relative_to(output.relative_to(root)).as_posix()
        if name in seen or not name.startswith('prereaction/independent_prereaction-') or not name.endswith('_raw.maegz'):
            raise ValueError('Unexpected or duplicate Glide raw subjob')
        seen.add(name)
        path = registered(name, raw['sha256'])
        with gzip.open(path, 'rb') as stream:
            data = stream.read(128 * 1024 * 1024 + 1)
        if len(data) > 128 * 1024 * 1024:
            raise ValueError('Glide raw subjob exceeds readback bound')
        supplier = Chem.MaeMolSupplier(io.BytesIO(data), sanitize=True, removeHs=False)
        index = 0
        while not supplier.atEnd():
            pose = next(supplier)
            if index >= 20 * len(expected):
                raise ValueError('Glide raw pose count exceeds approved bound')
            if pose is None or not pose.HasProp('_Name') or pose.GetProp('_Name') not in by_name:
                raise ValueError('Unreadable or foreign Glide pose')
            candidate = pose.GetProp('_Name')
            row = {'candidate_id': candidate, 'raw_file': name, 'pose_index': index,
                'identity_verified': False, 'structurally_eligible': False}
            try:
                row.update(measure(pose, originals[candidate], by_name[candidate], sulfur))
            except (ValueError, KeyError, RuntimeError, ZeroDivisionError) as error:
                row['deferred_reason'] = str(error)
            rows.append(row)
            index += 1
    if not seen or len(seen) > len(expected):
        raise ValueError('Glide raw subjob inventory is empty or exceeds its budget')
    states = [{**{key: row[key] for key in ('candidate_id', 'parent_id')},
        'control': row.get('role') == 'reference_control',
        **summarize([pose for pose in rows if pose['candidate_id'] == row['candidate_id']])} for row in expected]
    if any(state['pose_count'] > 20 for state in states):
        raise ValueError('Glide state exceeds frozen twenty-pose limit')
    for name, digest in hashes.items():
        if sha256_file(root / name) != digest:
            raise ValueError('Glide evidence changed during readback')
    return {'schema': 'denovo-batch-glide-review-v1', 'run_id': spec.run_id, 'states': states,
        'poses': rows, 'policy': POLICY, 'output_sha256': hashes,
        'scientific_acceptance': False, 'activity_validated': False, 'md_ready': False}
