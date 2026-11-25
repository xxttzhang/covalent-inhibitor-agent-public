"""Re-read the signed VS task's registered raw products and assess frozen gates."""
import json
from pathlib import Path

from rdkit import rdBase

from .covdock_vs import molecules, bound_file
from .covdock_vs_output import read as read_output
from .covdock_vs_preparation import revalidate, inspect_prepared
from .covdock_vs_geometry import Policy, measure
from .jobs import sha256_file
from .ligand_design import design_folder
from .ligand_first_inputs import load_campaign, build_parent_pose_mol
from .ligand_first_geometry import _strain_energy
from .ligand_refinement_target import encoded
from .ligand_vs_input import load as load_input
from .md_preparation_evidence import registered_output


def evaluate(root, state, manager, spec, result):
    from .source_release import verify_source
    verify_source(spec)
    if (spec.engine != 'schrodinger-covdock-vs' or spec.route != 'ligand-first'
            or spec.project_id != state['workflow_id'] or result.state != 'COMPLETED' or result.return_code != 0):
        raise ValueError('VS evaluation requires its completed registered current-study task')
    payload = load_input(root,state)
    packet = revalidate(root,spec.parameters['vs_preparation'])
    output = manager.registry.job_dir(spec.run_id)/'artifacts'
    hashes = {}
    def registered(name):
        path = registered_output(root,output,spec,result,name)
        hashes[str(path.relative_to(root))] = sha256_file(path)
        return path
    execution = json.loads(registered('vs-execution.json').read_text())
    if any(execution.get(key) != value for key,value in {
            'schema':'covdock-vs-execution-result-v1','run_id':spec.run_id,
            'engine':'schrodinger-covdock-vs','mode':'enrichment','state':'COMPLETED'}.items()):
        raise ValueError('VS execution receipt differs from the registered task')
    raw_path = execution['raw_output']['path']
    name = Path(raw_path).relative_to(output.relative_to(root)).as_posix()
    expected_name = 'docking/vs_' + sha256_file(manager.registry.job_dir(spec.run_id)/'spec.json')[:20] + '-out.maegz'
    if name != expected_name:
        raise ValueError('VS raw output does not have the original signed-job identity')
    raw = registered(name)
    if sha256_file(raw) != execution['raw_output']['sha256']:
        raise ValueError('VS raw output differs from its execution receipt')
    receptor = registered('prepared/receptor.mae')
    prepared = registered('prepared/ligands.mae')
    witness = json.loads(registered('prepared-input-witness.json').read_text())
    if witness != inspect_prepared(root,packet,output/'prepared'):
        raise ValueError('VS prepared input witness differs from current registered inputs')
    rows, counts = read_output(raw,molecules(root/packet['plan']['original_ligands'],False),
        molecules(prepared,True),molecules(receptor,True)[0],packet['plan']['attachment_residue'],
        expected_sha256=sha256_file(raw))
    identity = json.loads(registered('vs-output-identity.json').read_text())
    if identity != {'schema':'covdock-vs-output-identity-v1','rdkit_version':rdBase.rdkitVersion,
            'raw_sha256':sha256_file(raw), 'candidate_pose_counts':counts,
            'poses':[{key:row[key] for key in ('pose_index','candidate_id','input_index','score',
                'product_identity_verified','prepared_receptor_identity_verified')} for row in rows],
            'scientific_acceptance':False,'md_ready':False}:
        raise ValueError('VS raw structure re-read differs from registered identity measurements')
    directory = root/state['vs_input']['directory']
    policy = Policy.model_validate(json.loads((directory/'geometry-policy.json').read_text()))
    inventory = json.loads((directory/'receptor-inventory.json').read_text())
    design = design_folder(root,state['design_id'])
    campaign = load_campaign(design/'generation/campaign.json')
    parent = build_parent_pose_mol(campaign,design/'case')
    parent_energy = _strain_energy(parent)
    candidates = []
    for index,name in enumerate(payload['selected_candidate_ids']):
        measured, errors = [], []
        for row in (row for row in rows if row['candidate_id'] == name):
            try:
                measured.append(measure(row,campaign,parent,inventory,directory/'receptor-projection.pdb',policy,parent_energy))
            except (ValueError,KeyError,TypeError,RuntimeError) as error:
                errors.append({'pose_index':row['pose_index'],'error':str(error)})
        passed = [pose for pose in measured if pose['assessment']['review_eligible']]
        eligible = bool(passed) and not errors and len(measured) == counts[name]
        selected = min(passed,key=lambda pose:(float('inf') if pose['covdock_vs_score'] is None
            else pose['covdock_vs_score'], pose['metrics']['scaffold_rmsd_angstrom'],pose['pose_index'])) if eligible else None
        complete = bool(measured) and len(measured) == counts[name] and not errors and all(
            pose['assessment']['evidence_complete'] for pose in measured)
        candidates.append({'candidate_id':name,'input_index':index,'pose_count':counts[name],
            'status':'PASS' if eligible else 'FAIL' if complete else 'UNKNOWN' if counts[name] else 'NOT_EVALUATED',
            'review_eligible':eligible,'evidence_complete':complete,'passing_pose_count':len(passed),
            'selected_pose_index':selected['pose_index'] if selected else None, 'poses':measured,'errors':errors})
    # The raw products, approved inputs and frozen policy must still be intact
    # after measurement; do not persist an assessment of moving files.
    for name,checksum in {**spec.input_sha256,**hashes}.items():
        if sha256_file(bound_file(root,name)) != checksum:
            raise ValueError('VS evaluation source changed during measurement')
    return {'schema':'ligand-vs-scientific-review-v1','engine':spec.engine,'mode':'enrichment',
        'run_id':spec.run_id,'workflow_id':state['workflow_id'],'candidate_count':len(candidates),
        'candidates':candidates,'passing_candidate_count':sum(item['review_eligible'] for item in candidates),
        'policy':policy.model_dump(mode='json'),'input_sha256':spec.input_sha256,'output_sha256':hashes,
        'output_validation':'RAW_PRODUCT_IDENTITY_AND_GEOMETRY_REPLAYED',
        'reader_sha256':sha256_file(Path(__file__).with_name('covdock_vs_output.py')),
        'measurement_sha256':sha256_file(Path(__file__).with_name('covdock_vs_geometry.py')),
        'scientific_acceptance':False,'md_ready':False,
        'limitations':['Thresholds are provisional, not calibrated activity or reactivity predictions.',
            'N/O distance contacts are proxies, not assigned hydrogen bonds or energetic importance.',
            'Local relaxation energy is not global conformational strain or a binding free energy.',
            'Each pose uses its own receptor coordinates, aligned by deposited C-alpha atoms; no ligand fitting.',
            'A passing candidate is eligible for further review. MD requires separate graph, parameterization and preparation gates.']}


def review_reference(value):
    import hashlib
    raw = encoded(value)
    checksum = hashlib.sha256(raw).hexdigest()
    return {'path':'results/ligand-vs-reviews/'+checksum[:32]+'/review.json',
        'manifest_sha256':checksum,'artifact_id':'vs-review-'+checksum[:32],
        'run_id':value['run_id'],'passing_candidate_count':value['passing_candidate_count'],'md_ready':False}


def save_review(root,state,value):
    ref = review_reference(value)
    raw = encoded(value)
    path = root/ref['path']
    location = path.parent
    if any(path.is_symlink() for path in (root/'results',location.parent,location)):
        raise ValueError('VS review cannot traverse symbolic links')
    location.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        with path.open('xb') as stream:
            stream.write(raw)
    if path.read_bytes() != raw:
        raise ValueError('VS saved review changed')
    return ref


def load_review(root,state,manager,spec,result):
    from .source_release import verify_source
    verify_source(spec)
    load_input(root,state)
    revalidate(root,spec.parameters['vs_preparation'])
    ref = state['vs_execution']['evaluation_artifact']
    path = bound_file(root,ref['path'])
    if sha256_file(path) != ref['manifest_sha256']:
        raise ValueError('VS saved evaluation checksum changed')
    value = json.loads(path.read_text())
    if (result.state != 'COMPLETED' or result.return_code != 0
            or value['run_id'] != spec.run_id or value['workflow_id'] != state['workflow_id']
            or value['input_sha256'] != spec.input_sha256
            or value['reader_sha256'] != sha256_file(Path(__file__).with_name('covdock_vs_output.py'))
            or value['measurement_sha256'] != sha256_file(Path(__file__).with_name('covdock_vs_geometry.py'))):
        raise ValueError('VS evaluation no longer binds the original completed task or implementation')
    folder = manager.registry.job_dir(spec.run_id)/'artifacts'
    for name,checksum in value['output_sha256'].items():
        relative = Path(name).relative_to(folder.relative_to(root)).as_posix()
        registered_output(root,folder,spec,result,relative,checksum)
    for name,checksum in spec.input_sha256.items():
        if sha256_file(bound_file(root,name)) != checksum:
            raise ValueError('VS evaluated input changed')
    if review_reference(value) != ref:
        raise ValueError('VS evaluation artifact identity changed')
    return value


def report(root,ref,value):
    location = bound_file(root,ref['path']).parent
    lines = ['# CovDock-VS candidate review', '',
        'Registered run: '+value['run_id'], '',
        'This report re-reads registered raw Maestro products. Thresholds are provisional; MD admission remains pending.', '',
        '| Candidate | Poses | Jointly passing poses | Review status | Selected raw pose |',
        '|---|---:|---:|---|---:|']
    for candidate in value['candidates']:
        lines.append('| '+candidate['candidate_id']+' | '+str(candidate['pose_count'])+' | '+
            str(candidate['passing_pose_count'])+' | '+candidate['status']+' | '+str(candidate['selected_pose_index'])+' |')
    lines += ['', '## Per-pose evidence', '']
    for candidate in value['candidates']:
        lines += ['### '+candidate['candidate_id'], '']
        for pose in candidate['poses']:
            lines += ['Pose '+str(pose['pose_index'])+' — '+pose['assessment']['status'], '',
                '```json', json.dumps({'covdock_vs_score':pose['covdock_vs_score'],
                    'metrics':pose['metrics'],'assessment':pose['assessment']},ensure_ascii=False,indent=2),'```','']
        if candidate['errors']:
            lines += ['Evaluation errors (missing evidence, not measured molecular failure):',
                '```json',json.dumps(candidate['errors'],ensure_ascii=False,indent=2),'```','']
        if not candidate['pose_count']:
            lines += ['No output poses were produced for this approved candidate; it remains in the denominator.','']
    lines += ['## Frozen policy', '', '```json',json.dumps(value['policy'],indent=2),'```','',
              '## Limits', '', *['- '+text for text in value['limitations']], '',
              'Machine-readable review: `review.json`','']
    raw = '\n'.join(lines).encode()
    path = location/'report.md'
    if not path.exists():
        with path.open('xb') as stream:stream.write(raw)
    if path.is_symlink() or path.read_bytes() != raw:
        raise ValueError('VS report bytes changed')
    return {'path':str(path.relative_to(root)),'sha256':sha256_file(path),
            'review_artifact_id':ref['artifact_id'],'md_ready':False}
