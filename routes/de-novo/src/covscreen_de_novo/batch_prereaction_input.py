"""Fresh free-ligand conformers for the validated independent Glide SP route."""
import hashlib
from pathlib import Path
from rdkit import Chem, rdBase
from rdkit.Chem import AllChem
from .batch_lo_input import encoded

SEED = 2026111605


def freeze(root, lo_digest):
    """Freeze only freshly prepared post-LO priority inputs, never docking."""
    import json
    from .batch_lo import observe
    from .batch_lo_input import load
    from .batch_input import validate
    root = Path(root).resolve(strict=True)
    observed = observe(root, lo_digest, evaluate=True)
    payload, _ = load(root, lo_digest)
    manifest, _ = validate(root / 'state/de-novo-batches' / ('batch-' + payload['batch_manifest_sha256']),
        payload['batch_manifest_sha256'])
    originals = {row['candidate_id']: row for row in manifest['candidates']}
    priority = observed['prereaction_priority']
    selected = [originals[row['candidate_id']] for row in priority['selected']]
    if not selected:
        raise ValueError('No physically passing LO candidates for prereaction preparation')
    receptors = [name for name in payload['lineage_sha256'] if name.endswith('/prepared/receptor.mae')]
    if len(receptors) != 1:
        raise ValueError('Prereaction grid requires the unique independently prepared unreacted VS receptor')
    receptor = receptors[0]
    route = Path(__file__).resolve().parents[2]
    reference_sources = {name: hashlib.sha256((route / name).read_bytes()).hexdigest() for name in
        ('evaluation/control.py', 'registry/egfr_c797_v1/assets/0WM.cif',
         'registry/egfr_c797_v1/assets/0WN.cif', 'registry/egfr_c797_v1/assets/4G5J.pdb',
         'src/covscreen_de_novo/batch_glide_recipe.py', 'src/covscreen_de_novo/batch_control_states.py',
         'src/covscreen_de_novo/batch_prereaction_geometry.py')}
    source = {'lo_manifest_sha256': lo_digest, 'lo_run_id': observed['run_id'],
        'evaluation_artifact': observed['evaluation_artifact'], 'priority': priority,
        'rdkit_version': rdBase.rdkitVersion, 'seed': SEED,
        'implementation_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'reference_sources': reference_sources, 'receptor': receptor,
        'receptor_sha256': payload['lineage_sha256'][receptor]}
    digest = hashlib.sha256(encoded(source)).hexdigest()
    folder = root / 'state/de-novo-prereaction-inputs' / digest
    saved = folder / 'input.json'
    if any(path.is_symlink() for path in (saved, *saved.parents)):
        raise ValueError('Prereaction preparation cannot traverse symlinks')
    if not saved.exists():
        result = export(selected, folder)
        reference = prepare_reference(folder / 'reference')
        from .batch_glide_recipe import grid_input
        grid_recipe = grid_input(root / receptor, reference['grid_center_from_crystal_adduct'])
        (folder / 'prereaction_grid.in').write_text(grid_recipe)
        value = {**result, 'source': source, 'task_id': payload['task_id'],
            'workflow_id': payload['workflow_id'], 'directory': str(folder.relative_to(root)),
            'source_sha256': digest, 'reference': reference, 'reference_controls_prepared': True,
            'control_ligprep_performed': False, 'grid_prepared': False,
            'grid_recipe_sha256': hashlib.sha256(grid_recipe.encode()).hexdigest()}
        with saved.open('xb') as stream:
            stream.write(encoded(value))
    value = json.loads(saved.read_text())
    if value['source'] != source or value['source_sha256'] != digest:
        raise ValueError('Frozen prereaction source changed')
    if [row['candidate_id'] for row in value['states']] != [row['candidate_id'] for row in selected]:
        raise ValueError('Prereaction candidate denominator changed')
    for index, row in enumerate(value['states']):
        if row['state'] == 'DEFERRED':
            continue
        path = folder / f'candidate_{index:03d}.sdf'
        if (row['file'] != path.name or path.is_symlink() or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']):
            raise ValueError('Frozen prereaction ligand changed')
    control = folder / 'reference/fresh_afatinib.sdf'
    if (control.is_symlink() or not control.is_file()
            or hashlib.sha256(control.read_bytes()).hexdigest() != value['reference']['sha256']):
        raise ValueError('Frozen reference precursor changed')
    recipe = folder / 'prereaction_grid.in'
    if recipe.is_symlink() or hashlib.sha256(recipe.read_bytes()).hexdigest() != value['grid_recipe_sha256']:
        raise ValueError('Frozen prereaction grid recipe changed')
    return {**value, 'manifest_sha256': hashlib.sha256(saved.read_bytes()).hexdigest()}


def load(root, source_digest, manifest_digest):
    """Revalidate an existing exact manifest before reconstructing source evidence."""
    import json
    import re
    if any(not isinstance(v, str) or not re.fullmatch('[a-f0-9]{64}', v)
           for v in (source_digest, manifest_digest)):
        raise ValueError('Exact prereaction source and manifest digests required')
    root = Path(root).resolve(strict=True)
    folder = root / 'state/de-novo-prereaction-inputs' / source_digest
    path = folder / 'input.json'
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError('Prereaction input must already exist as regular data')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest_digest:
        raise ValueError('Prereaction manifest differs from its frozen approval input')
    value = json.loads(raw)
    if (value['schema'] != 'denovo-batch-prereaction-input-v1' or value['source_sha256'] != source_digest
            or hashlib.sha256(encoded(value['source'])).hexdigest() != source_digest):
        raise ValueError('Prereaction source identity changed')
    verified = freeze(root, value['source']['lo_manifest_sha256'])
    if verified != {**value, 'manifest_sha256': manifest_digest}:
        raise ValueError('Prereaction inputs no longer match current registered LO evidence')
    return verified, folder


def identity(molecule):
    molecule = Chem.RemoveHs(Chem.Mol(molecule))
    for atom in molecule.GetAtoms():
        atom.SetAtomMapNum(0)
    return Chem.MolToSmiles(molecule, isomericSmiles=True)


def fresh(original, *, control=False):
    molecule = Chem.MolFromSmiles(original['mapped_smiles'])
    if molecule is None:
        raise ValueError('Invalid prereaction molecular graph')
    expected = identity(molecule)
    molecule.RemoveAllConformers()
    for atom in molecule.GetAtoms():
        atom.SetAtomMapNum(0)
    molecule = Chem.AddHs(molecule)
    parameters = AllChem.ETKDGv3()
    parameters.randomSeed = SEED
    parameters.numThreads = 1
    if AllChem.EmbedMolecule(molecule, parameters) != 0:
        raise ValueError('Independent prereaction embedding failed')
    probe = Chem.Mol(molecule)
    Chem.AssignStereochemistryFrom3D(probe, replaceExistingTags=True)
    if identity(molecule) != expected or identity(probe) != expected:
        raise ValueError('Fresh prereaction graph or stereochemistry differs')
    motif = Chem.MolFromSmarts('[C;H1,H2:1]=[CH:2]-[C:3](=[O:4])-[N:5]' if control
        else '[CH2:1]=[CH:2]-[C:3](=[O:4])-[N:5]')
    hits = molecule.GetSubstructMatches(motif)
    if len(hits) != 1:
        raise ValueError('Prereaction terminal acrylamide is not unique')
    molecule.SetProp('_Name', original['candidate_id'])
    molecule.SetProp('PARENT_CANDIDATE_ID', original['parent_id'])
    molecule.SetProp('COORDINATE_ORIGIN', f'independent_ETKDGv3_seed{SEED}')
    return molecule, {'candidate_id': original['candidate_id'], 'parent_id': original['parent_id'],
        'canonical_smiles': expected, 'beta_index_0based': hits[0][0], 'alpha_index_0based': hits[0][1],
        'seed': SEED, 'generated_coordinates_used': False, 'lo_coordinates_used': False}


def prepare_reference(output):
    """Rebuild the official afatinib precursor; crystal adduct only locates grid."""
    import importlib.util
    route = Path(__file__).resolve().parents[2]
    script = route / 'evaluation/control.py'
    assets = route / 'registry/egfr_c797_v1/assets'
    specification = importlib.util.spec_from_file_location('denovo_registered_control', script)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    precursor = assets / '0WM.cif'
    reference = module.prepare_control(assets, precursor, hashlib.sha256(precursor.read_bytes()).hexdigest(), Path(output))
    original = {'candidate_id': 'CONTROL_AFATINIB', 'parent_id': 'CONTROL_AFATINIB',
        'mapped_smiles': reference['record']['smiles']}
    molecule, record = fresh(original, control=True)
    path = Path(output) / 'fresh_afatinib.sdf'
    with Chem.SDWriter(str(path)) as writer:
        writer.write(molecule)
    readback = list(Chem.SDMolSupplier(str(path), removeHs=False))
    if len(readback) != 1 or readback[0] is None or identity(readback[0]) != record['canonical_smiles']:
        raise ValueError('Fresh reference control identity changed')
    crystal = next(Chem.SDMolSupplier(str(Path(output) / 'control_crystal_adduct.sdf'), removeHs=False))
    if crystal is None:
        raise ValueError('Reference grid ligand is unreadable')
    points = crystal.GetConformer().GetPositions()
    center = [float(sum(point[axis] for point in points) / len(points)) for axis in range(3)]
    return {**record, 'role': 'reference_control', 'file': path.name,
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'control_source': reference['sources'],
        'grid_center_from_crystal_adduct': center, 'prereaction_pose_reference_available': False,
        'control_script_sha256': hashlib.sha256(script.read_bytes()).hexdigest()}


def export(originals, output):
    if not 1 <= len(originals) <= 5 or len({r['candidate_id'] for r in originals}) != len(originals):
        raise ValueError('Prepare one to five distinct post-LO priority candidates')
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for index, original in enumerate(originals):
        try:
            molecule, row = fresh(original)
            path = output / f'candidate_{index:03d}.sdf'
            with Chem.SDWriter(str(path)) as writer:
                writer.write(molecule)
            loaded = list(Chem.SDMolSupplier(str(path), removeHs=False))
            if len(loaded) != 1 or loaded[0] is None or identity(loaded[0]) != row['canonical_smiles']:
                raise ValueError('Fresh prereaction serialization changed identity')
            rows.append({**row, 'state': 'PREPARED_NOT_DOCKED', 'file': path.name,
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        except (ValueError, RuntimeError) as error:
            rows.append({'candidate_id': original['candidate_id'], 'parent_id': original['parent_id'],
                'state': 'DEFERRED', 'reason': str(error)})
    return {'schema': 'denovo-batch-prereaction-input-v1', 'rdkit_version': rdBase.rdkitVersion,
        'states': rows, 'compute_submitted': False, 'docking_performed': False}
