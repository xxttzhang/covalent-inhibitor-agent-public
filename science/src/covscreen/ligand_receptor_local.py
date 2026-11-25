"""Derive a bounded local proposal from a registered failed receptor task."""
import json
import math
from .execution import JobManager
from .jobs import sha256_file
from .ligand_receptor_input import validate_binding
from .ligand_receptor_source import context
from .md_preparation_evidence import registered_output
from .pdb_coordinates import read_pdb_coordinates
from .receptor_local_policy import make_policy


def derive_policy(root, state, record):
    loaded = validate_binding(root, state)
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(record['run_id']), manager.registry.load_result(record['run_id'])
    from .native_approval import verify_spec, decode
    from .approval_scope import normalize_spec
    verify_spec(spec)
    scope = record['approved_scope']
    if (normalize_spec(spec) != scope['spec']
            or decode(decode(spec.provenance['native_approval'])['approval'])['scope'] != scope
            or spec.project_id != state['design_id']
            or spec.engine != 'library-first-receptor-preparation' or spec.route != 'ligand-first'
            or any(getattr(result, k) != getattr(spec, k) for k in ('run_id','project_id','engine','route','attempt','parent_run_id'))):
        raise ValueError('Local proposal predecessor differs from its approved current-study task')
    if result.state != 'FAILED' or spec.parameters.get('relaxation_policy_version', 'v1') != 'v1':
        raise ValueError('Local repair proposal requires a registered failed fixed-observation v1 task')
    folder = manager.registry.job_dir(record['run_id']) / 'artifacts/receptor-preparation'
    pipeline_path = registered_output(root, folder, spec, result, 'pipeline.json')
    pipeline = json.loads(pipeline_path.read_text())
    if (pipeline.get('schema') != 'receptor-preparation-pipeline-v1'
            or pipeline.get('status') != 'preparation_failed'
            or pipeline.get('target_acquisition_sha256') != spec.parameters['manifest_sha256']
            or spec.parameters['manifest_sha256'] != context(state)['receptor_input']['manifest_sha256']):
        raise ValueError('Local repair failure receipt differs from the current frozen source')
    witnesses = {'pipeline.json': sha256_file(pipeline_path)}
    def read(name):
        path = registered_output(root, folder, spec, result, name, pipeline['artifacts'][name])
        witnesses[name] = sha256_file(path)
        return json.loads(path.read_text())
    geometry, quality = read('geometry-quality.json'), read('identity-quality.json')
    if quality.get('status') != 'identity_and_stereochemistry_passed_geometry_pending':
        raise ValueError('Local collision repair cannot replace failed identity/stereochemistry review')
    inventory, parent = read('heavy/inventory.json'), read('heavy/atom-mapping.json')
    final = read('relaxation/heavy-atom-mapping.json')['atoms']
    mapping, = loaded['acquisition']['acquisition']['mappings']
    source = loaded['acquisition_directory'] / 'structure.pdb'
    source_sha = sha256_file(source)
    if inventory['source_sha256'] != source_sha:
        raise ValueError('Failed task source differs from the current receptor')
    _, _, deposited, _ = read_pdb_coordinates(source, mapping['entry_id'],
        loaded['acquisition']['selection'].get('model'))
    by_serial = {a['serial']: a for a in deposited}
    observed = [a for a in parent['atoms'] if a['original_serial'] is not None]
    for atom in observed:
        original = by_serial.get(atom['original_serial'])
        if (original is None or any(original[k] != atom[v] for k,v in
                [('name','name'),('chain','chain'),('residue','author_residue'),('resname','resname')])
                or math.dist(original['xyz'], atom['xyz_angstrom']) > 1e-10):
            raise ValueError('Local repair atom identity is not the original observed coordinate')
    original_by_key = {(a['entity_position'], a['name']): a for a in observed}
    final_by_key = {(a['entity_position'], a['name']): a for a in final}
    pairs = list(geometry.get('vdw_clashes', []))
    internal = geometry.get('distance_checks', {}).get('internal', {})
    if internal.get('violation_count', 0) > len(internal.get('first_100_violations', [])):
        raise ValueError('Local proposal requires the full internal collision inventory')
    pairs += internal.get('first_100_violations', [])
    positions = set()
    for pair in pairs:
        if pair.get('involves_modeled_atom') is not False:
            raise ValueError('Local observed repair requires observed-only source collision pairs')
        keys = [tuple(k) for k in pair['atoms']]
        if len(keys) != 2 or any(k not in original_by_key or k not in final_by_key for k in keys):
            raise ValueError('Local collision atom mapping is incomplete')
        for key in keys:
            if math.dist(original_by_key[key]['xyz_angstrom'], final_by_key[key]['xyz_angstrom']) > 1e-10:
                raise ValueError('Source collision coordinates were not retained')
        distance = math.dist(*(original_by_key[k]['xyz_angstrom'] for k in keys))
        if abs(distance - pair['distance_angstrom']) > 1e-8:
            raise ValueError('Registered collision does not reproduce from original coordinates')
        positions.update(k[0] for k in keys)
    if not positions:
        raise ValueError('No observed collision supports local repair')
    component = loaded['acquisition']['acquisition']['plan']['component']
    ligands = [a for a in deposited if a['record']=='HETATM' and a['resname']==component and a['element'] not in {'H','D'}]
    if len({(a['chain'],a['residue']) for a in ligands}) != 1 or any(a['altloc'] for a in ligands):
        raise ValueError('Local pocket definition requires one unambiguous deposited parent ligand')
    pocket = {a['entity_position'] for a in observed if min(math.dist(a['xyz_angstrom'], l['xyz']) for l in ligands) <= 5.0}
    target = mapping['entity_seq_id']; pocket.add(target)
    policy = make_policy(source_sha, observed, positions, pocket, target)
    return policy, {'previous_run_id': record['run_id'], 'source_sha256': source_sha,
        'registered_outputs': witnesses, 'pocket_component': component,
        'pocket_definition': 'observed residues within 5 angstrom of deposited parent heavy atoms plus target Cys',
        'observed_atom_count': len(observed), 'movable_observed_atom_count': len(policy['local_observation_mask']['atoms']),
        'scientific_acceptance': 'not_established', 'compute_submitted': False}


def derive_bounded_policy(root, state, record):
    """Replan only a signed failed harmonic-local task with verified motion failure."""
    from .native_approval import verify_spec, decode
    from .approval_scope import normalize_spec
    from .receptor_local_policy import make_bounded_policy, validate_policy_version, digest, audit_motion
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(record['run_id']), manager.registry.load_result(record['run_id'])
    verify_spec(spec)
    scope = record['approved_scope']
    if (normalize_spec(spec) != scope['spec']
            or decode(decode(spec.provenance['native_approval'])['approval'])['scope'] != scope
            or spec.project_id != state['design_id'] or spec.engine != 'library-first-receptor-preparation'
            or spec.route != 'ligand-first' or result.state != 'FAILED'
            or spec.parameters.get('relaxation_policy_version') != 'v2-local'
            or any(getattr(result, k) != getattr(spec, k) for k in ('run_id','project_id','engine','route','attempt','parent_run_id'))):
        raise ValueError('Bounded replanning requires its approved failed harmonic-local task')
    policy = spec.parameters['relaxation_policy']
    validate_policy_version(policy, 'v2-local')
    if digest(policy) != spec.parameters['relaxation_policy_sha256']:
        raise ValueError('Predecessor local policy digest differs')
    handoff = context(state)
    if spec.parameters['manifest_sha256'] != handoff['receptor_input']['manifest_sha256']:
        raise ValueError('Bounded replanning cannot change receptor source')
    # Find the original immutable v1 collision witness, not the moved local output.
    candidates = []
    for previous in handoff.get('receptor_preparation_history', []):
        previous_spec = manager.registry.load_spec(previous['run_id'])
        if previous_spec.parameters.get('relaxation_policy_version', 'v1') == 'v1':
            original_policy, evidence = derive_policy(root, state, previous)
            if original_policy == policy:
                candidates.append((previous, evidence))
    if len(candidates) != 1:
        raise ValueError('Bounded replanning requires a unique retained original source-collision witness')
    previous, source_evidence = candidates[0]
    folder = manager.registry.job_dir(record['run_id']) / 'artifacts/receptor-preparation'
    pipeline_path = registered_output(root, folder, spec, result, 'pipeline.json')
    pipeline = json.loads(pipeline_path.read_text())
    if (pipeline.get('schema') != 'receptor-preparation-pipeline-v2'
            or pipeline.get('status') != 'preparation_failed'
            or pipeline.get('target_acquisition_sha256') != spec.parameters['manifest_sha256']
            or pipeline.get('relaxation_policy') != policy):
        raise ValueError('Local failed pipeline differs from the original approved scope')
    witnesses = {'pipeline.json': sha256_file(pipeline_path)}
    def read(name):
        path = registered_output(root, folder, spec, result, name, pipeline['artifacts'][name])
        witnesses[name] = sha256_file(path)
        return json.loads(path.read_text())
    quality = read('identity-quality.json')
    parent = read('heavy/atom-mapping.json')['atoms']
    final = read('relaxation/heavy-atom-mapping.json')['atoms']
    # Rebind every retained coordinate to the verified v1 source inventory.
    original_spec = manager.registry.load_spec(previous['run_id'])
    original_result = manager.registry.load_result(previous['run_id'])
    original_folder = manager.registry.job_dir(previous['run_id']) / 'artifacts/receptor-preparation'
    original_path = registered_output(root, original_folder, original_spec, original_result,
                                     'heavy/atom-mapping.json', source_evidence['registered_outputs']['heavy/atom-mapping.json'])
    fields = ('entity_position','name','chain','author_residue','resname','original_serial','xyz_angstrom')
    def retained(rows):
        return sorted([{k: a[k] for k in fields} for a in rows if a['original_serial'] is not None],
                      key=lambda a: (a['entity_position'],a['name']))
    if retained(parent) != retained(json.loads(original_path.read_text())['atoms']):
        raise ValueError('Local predecessor changed original observed coordinates')
    audit = audit_motion(policy, parent, final, source_evidence['source_sha256'])
    if (quality.get('status') != 'local_motion_limits_failed'
            or audit != quality.get('local_motion_audit') or not audit['violations']
            or any(not row['movable'] for row in audit['violations'])
            or any(quality.get(k) != [] for k in ('provenance_violations','missing_atoms','extra_atoms',
                   'missing_bonds','extra_bonds','element_mismatches','stereochemistry_violations'))):
        raise ValueError('Bounded replanning requires isolated movable-atom displacement failure')
    return make_bounded_policy(policy), {'previous_run_id': record['run_id'],
        'source_collision_evidence': source_evidence, 'registered_outputs': witnesses,
        'predecessor_policy_sha256': digest(policy), 'motion_violation_count': len(audit['violations']),
        'fixed_atoms_unchanged': True, 'compute_submitted': False, 'scientific_acceptance': 'not_established'}


def derive_block_policy(root, state, record):
    """Replan a signed bounded iteration-limit failure; preserve source and history."""
    from .native_approval import verify_spec, decode
    from .approval_scope import normalize_spec
    from .receptor_local_policy import make_block_policy, validate_policy_version, digest
    manager = JobManager(root)
    spec = manager.registry.load_spec(record['run_id'])
    result = manager.registry.load_result(record['run_id'])
    verify_spec(spec)
    scope = record['approved_scope']
    if (normalize_spec(spec) != scope['spec']
            or decode(decode(spec.provenance['native_approval'])['approval'])['scope'] != scope
            or spec.project_id != state['design_id'] or spec.engine != 'library-first-receptor-preparation'
            or spec.route != 'ligand-first' or result.state != 'FAILED'
            or spec.parameters.get('relaxation_policy_version') != 'v2-local-bounded'
            or any(getattr(result,k) != getattr(spec,k) for k in ('run_id','project_id','engine','route','attempt','parent_run_id'))):
        raise ValueError('Block replanning requires its approved failed bounded task')
    policy = spec.parameters['relaxation_policy']
    validate_policy_version(policy, 'v2-local-bounded')
    handoff = context(state)
    if (digest(policy) != spec.parameters['relaxation_policy_sha256']
            or spec.parameters['manifest_sha256'] != handoff['receptor_input']['manifest_sha256']):
        raise ValueError('Block predecessor policy or source binding differs')
    candidates = []
    for previous in handoff.get('receptor_preparation_history', []):
        previous_spec = manager.registry.load_spec(previous['run_id'])
        if previous_spec.parameters.get('relaxation_policy_version') == 'v2-local':
            derived, evidence = derive_bounded_policy(root, state, previous)
            if derived == policy:
                candidates.append((previous, evidence))
    if len(candidates) != 1:
        raise ValueError('Block replanning requires unique original local and source-collision witnesses')
    previous, source_evidence = candidates[0]
    folder = manager.registry.job_dir(spec.run_id) / 'artifacts/receptor-preparation'
    path = registered_output(root,folder,spec,result,'pipeline.json')
    pipeline = json.loads(path.read_text())
    if (pipeline.get('schema') != 'receptor-preparation-pipeline-v2'
            or pipeline.get('status') != 'preparation_failed'
            or pipeline.get('receptor_prepared') is not False
            or pipeline.get('target_acquisition_sha256') != spec.parameters['manifest_sha256']
            or pipeline.get('relaxation_policy') != policy):
        raise ValueError('Bounded failure pipeline differs from its approved source and policy')
    witnesses = {'pipeline.json':sha256_file(path)}
    def read(name):
        path = registered_output(root,folder,spec,result,name,pipeline['artifacts'][name])
        witnesses[name] = sha256_file(path)
        return json.loads(path.read_text())
    receipt = read('relaxation/receipt.json')
    stages = receipt.get('stages',[])
    if (receipt.get('schema') != 'receptor-relaxation-receipt-v2'
            or receipt.get('status') != 'relaxation_failed'
            or receipt.get('policy') != policy or receipt.get('policy_sha256') != digest(policy)
            or receipt.get('source_sha256') != policy['local_observation_mask']['source_sha256']
            or [s.get('name') for s in stages] != [s['name'] for s in policy['stages']]):
        raise ValueError('Bounded failure relaxation receipt differs from the approved plan')
    numerical = stages[-1].get('bounded_optimizer',{})
    config = policy['bounded_optimizer']
    energies = numerical.get('accepted_energy_history_kj_mol',[])
    residual = numerical.get('projected_gradient_max_kj_mol_nm')
    if (numerical.get('status') != 'iteration_limit' or numerical.get('scientific_acceptance') is not False
            or type(numerical.get('iterations')) is not int or numerical['iterations'] != config['max_iterations']
            or type(numerical.get('evaluations')) is not int
            or not numerical['iterations']+1 <= numerical['evaluations'] <= config['max_evaluations']
            or type(residual) not in (float,int) or not math.isfinite(residual) or residual <= config['tolerance']
            or not isinstance(energies,list) or len(energies) != numerical['iterations']+1
            or any(type(e) not in (float,int) or not math.isfinite(e) for e in energies)
            or any(b>a for a,b in zip(energies,energies[1:]))
            or numerical.get('energy_kj_mol') != energies[-1]):
        raise ValueError('Block recovery requires a verified bounded iteration-limit failure')
    parent = read('heavy/atom-mapping.json')['atoms']
    previous_spec = manager.registry.load_spec(previous['run_id'])
    previous_result = manager.registry.load_result(previous['run_id'])
    previous_folder = manager.registry.job_dir(previous['run_id']) / 'artifacts/receptor-preparation'
    original = registered_output(root,previous_folder,previous_spec,previous_result,
        'heavy/atom-mapping.json',source_evidence['registered_outputs']['heavy/atom-mapping.json'])
    fields = ('entity_position','name','chain','author_residue','resname','original_serial','xyz_angstrom')
    def retained(rows):
        return sorted([{k:a[k] for k in fields} for a in rows if a['original_serial'] is not None],
                      key=lambda a:(a['entity_position'],a['name']))
    if retained(parent) != retained(json.loads(original.read_text())['atoms']):
        raise ValueError('Bounded predecessor changed the original observed coordinates')
    return make_block_policy(policy), dict(previous_run_id=spec.run_id,
        predecessor_policy_sha256=digest(policy),source_collision_evidence=source_evidence,
        registered_outputs=witnesses,iteration_limit=numerical['iterations'],
        projected_gradient_max_kj_mol_nm=residual,compute_submitted=False,
        scientific_acceptance='not_established')
