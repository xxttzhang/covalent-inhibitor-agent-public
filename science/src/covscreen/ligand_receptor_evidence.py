"""Study-bound prepared-receptor readback using the shared scientific audits."""
import hashlib
import json
from pathlib import Path

from . import receptor_evidence
from .ligand_receptor_source import context as receptor_context, pre_vs, source_input
from .jobs import ArtifactRegistry, sha256_file
from .md_preparation_evidence import registered_output


def failure_diagnostics(root, state, manager, spec, result):
    """Read only registered failure evidence, without admitting or rerunning it."""
    folder = manager.registry.job_dir(spec.run_id) / 'artifacts/receptor-preparation'
    output = {'run_id': spec.run_id, 'scope': 'registered_producer_failure_diagnostics',
        'job_state': result.state, 'return_code': result.return_code,
        'failure_reason': result.failure_reason, 'pipeline_available': False,
        'sources': [], 'receptor_prepared': False, 'md_ready': False,
        'interpretation': 'Recorded producer checks, not an independent scientific replay or molecular inactivity.',
        'recovery': 'Keep this task and its evidence. Use workflow reconsider with a reason to revise the source; '
                    'new computation requires a new plan and separate native approval. No automatic retry.'}
    relative = str((folder / 'pipeline.json').relative_to(root))
    if not any(item.path == relative for item in result.artifacts):
        output['unavailable_reason'] = 'No registered pipeline receipt; the exit code alone does not establish the cause.'
        return output

    def read(name, digest=None):
        path = registered_output(root, folder, spec, result, name, digest)
        if path.stat().st_size > 2_000_000:
            raise ValueError('Receptor diagnostic artifact exceeds the bounded reader limit')
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError('Receptor diagnostic artifact must be an object')
        output['sources'].append({'path': str(path.relative_to(root)), 'sha256': sha256_file(path)})
        return value

    pipeline = read('pipeline.json')
    binding = receptor_context(state)['receptor_input']
    restrained = spec.parameters.get('relaxation_policy_version') in {'v2', 'v2-local', 'v2-local-bounded', 'v2-local-block'}
    version = 'v2' if restrained else 'v1'
    if restrained and pipeline.get('relaxation_policy') != spec.parameters.get('relaxation_policy'):
        raise ValueError('Failure receipt relaxation policy differs from the approved plan')
    if (pipeline.get('schema') != 'receptor-preparation-pipeline-' + version
            or pipeline.get('target_acquisition_sha256') != binding['manifest_sha256']
            or pipeline.get('receptor_prepared') is not False
            or not isinstance(pipeline.get('artifacts'), dict)
            or not isinstance(pipeline.get('phases'), list)):
        raise ValueError('Failure receipt differs from the current receptor input or failure state')
    output.update(pipeline_available=True, status=pipeline.get('status'),
        error=str(pipeline.get('error', ''))[:2000],
        phases=[{key: phase.get(key) for key in ('phase', 'status')} for phase in pipeline['phases'][:20]],
        phase_count=len(pipeline['phases']))
    relaxation_digest = pipeline['artifacts'].get('relaxation/receipt.json')
    if relaxation_digest is not None:
        from .receptor_relaxation import RELAXATION_POLICY_V1
        relaxation = read('relaxation/receipt.json', relaxation_digest)
        expected_policy = spec.parameters.get('relaxation_policy', RELAXATION_POLICY_V1)
        policy_digest = hashlib.sha256(json.dumps(expected_policy, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if (relaxation.get('schema') != 'receptor-relaxation-receipt-' + version
                or relaxation.get('policy') != expected_policy
                or relaxation.get('policy_sha256') != policy_digest
                or not isinstance(relaxation.get('stages'), list)):
            raise ValueError('Failed relaxation receipt differs from the approved policy')
        stages = relaxation['stages']
        if len(stages) > len(expected_policy['stages']) or any(
                not isinstance(stage, dict) or stage.get('name') != expected_policy['stages'][i]['name'] for i, stage in enumerate(stages)):
            raise ValueError('Failed relaxation stage order differs from the approved policy')
        last = stages[-1] if stages else {}
        detail = {'status': relaxation.get('status'), 'error': str(relaxation.get('error', ''))[:1000],
            'stage_count': len(stages), 'last_stage': {k: last.get(k) for k in
                ('name', 'tolerance_reached', 'iteration_cap', 'convergence_metric',
                 'movable_force_rms_component_kj_mol_nm', 'fixed_maximum_drift_angstrom',
                 'observed_maximum_displacement_angstrom', 'fixed_observed_atom_count',
                 'movable_observed_atom_count')},
            'coordinate_identity_available': False,
            'interpretation': 'Registered producer termination measurements, not independent geometry or motion acceptance.'}
        stage_binding = relaxation.get('stage_coordinate_identity')
        if stage_binding is not None:
            if not isinstance(stage_binding, dict):
                raise ValueError('Invalid stage coordinate identity binding')
            for name, key in [('stage-atom-mapping.json','atom_mapping_sha256'),
                              ('stage-topology.json','topology_sha256')]:
                digest = stage_binding.get(key)
                if not isinstance(digest, str) or relaxation.get('artifacts', {}).get(name) != digest:
                    raise ValueError('Stage identity checksum differs from the relaxation receipt')
                path = registered_output(root, folder, spec, result, 'relaxation/' + name, digest)
                output['sources'].append({'path': str(path.relative_to(root)), 'sha256': sha256_file(path)})
            detail['coordinate_identity_available'] = True
        optimizer = last.get('bounded_optimizer')
        if optimizer is not None:
            import math
            config = expected_policy.get('bounded_optimizer')
            if not isinstance(config, dict) or not isinstance(optimizer, dict):
                raise ValueError('Bounded termination diagnostics require the approved optimizer')
            from .receptor_local_policy import BLOCK_OPTIMIZER
            block = config == BLOCK_OPTIMIZER
            residual = optimizer.get('projected_gradient_max_kj_mol_nm')
            unavailable = (block and optimizer.get('full_residual_available') is False
                           and residual is None and optimizer.get('status') != 'converged')
            if block and type(optimizer.get('full_residual_available')) is not bool:
                raise ValueError('Block diagnostics must distinguish full residual availability')
            if (optimizer.get('status') not in {'converged','iteration_limit','evaluation_limit',
                                               'line_search_stalled','line_search_failed'}
                    or optimizer.get('scientific_acceptance') is not False
                    or type(optimizer.get('iterations')) is not int
                    or not 0 <= optimizer['iterations'] <= config['max_iterations']
                    or type(optimizer.get('evaluations')) is not int
                    or not 1 <= optimizer['evaluations'] <= config['max_evaluations']
                    or not unavailable and (type(residual) not in (int, float)
                                            or not math.isfinite(residual) or residual < 0)):
                raise ValueError('Invalid bounded optimizer termination diagnostics')
            detail['bounded_optimizer'] = {k: optimizer[k] for k in ('status','iterations','evaluations',
                'projected_gradient_max_kj_mol_nm','scientific_acceptance')}
            detail['bounded_optimizer']['required_tolerance_kj_mol_nm'] = config['tolerance']
            if block:
                detail['bounded_optimizer']['full_residual_available'] = optimizer['full_residual_available']
        output['relaxation'] = detail
        if relaxation.get('status') == 'relaxation_failed':
            output['pipeline_error'] = output['error']
            output['error'] = detail['error']
            output['failure_stage'] = 'relaxation'
    for name in ('identity-quality.json', 'geometry-quality.json'):
        digest = pipeline['artifacts'].get(name)
        if digest is None:
            continue
        if not isinstance(digest, str):
            raise ValueError('Diagnostic artifact checksum is missing')
        value = read(name, digest)
        if name == 'identity-quality.json':
            output['identity_quality'] = {'status': value.get('status')}
            if value.get('local_motion_audit'):
                local = value['local_motion_audit']
                output['identity_quality']['local_motion_audit'] = {
                    key: local.get(key) for key in ('status', 'policy_sha256', 'observed_atom_count',
                        'movable_observed_atom_count', 'fixed_observed_atom_count')}
                output['identity_quality']['local_motion_audit'].update(
                    violation_count=len(local.get('violations', [])),
                    first_10_violations=local.get('violations', [])[:10])
            continue
        if value.get('schema') != 'receptor-geometry-audit-' + version:
            raise ValueError('Unexpected receptor geometry diagnostic schema')
        checks = value.get('distance_checks', {})
        output['geometry_quality'] = {
            'status': value.get('status'), 'policy': value.get('policy'),
            'distance_checks': {name: {key: row.get(key) for key in (
                'status', 'tested', 'violation_count', 'modeled_pair_violations',
                'retained_observed_pair_violations')} | {
                    'violation_examples': row.get('first_100_violations', [])[:10]}
                for name, row in checks.items() if name in {'bonds', 'angles', 'internal'}},
            'vdw_clash_count': len(value.get('vdw_clashes', [])),
            'vdw_clash_examples': value.get('vdw_clashes', [])[:10],
            'aromatic_planarity_violation_count': len(value.get('aromatic_planarity_violations', [])),
            'reactive_site_prereaction_state': value.get('reactive_site_prereaction_state'),
            'interpretation_contract': {
                'involves_modeled_atom': 'True means at least one atom was modeled rather than retained '
                    'from observed coordinates. False means both atoms are retained observed atoms; '
                    'it does not mean preparation-generated atoms.',
                'atom_identifiers': 'Example pairs use entity_position and atom name, not author residue '
                    'numbers. Preserve these identifiers exactly; do not infer residue identities.',
                'bounds_angstrom': 'Reference bounds, not direct acceptance cutoffs. The recorded '
                    'relative tolerance applies. Internal checks apply the lower bound only.',
                'causal_scope': 'These records locate failed checks; they do not establish why the '
                    'source contains a clash or prove that preparation introduced it.',
                'recovery_scope': 'The registered v1 relaxation policy fixes retained observed heavy '
                    'atoms. Repeating that policy cannot resolve a retained-observed pair clash. '
                    'A justified source revision or separately implemented and validated preparation '
                    'policy is needed; neither is performed or approved by this diagnostic.',
            },
            'example_limit': 10,
        }
        if restrained:
            diagnostic = output['geometry_quality']
            for name, row in diagnostic['distance_checks'].items():
                row.pop('retained_observed_pair_violations', None)
                row['observed_origin_pair_violations'] = checks[name].get('observed_origin_pair_violations')
            diagnostic['interpretation_contract'].update(
                involves_modeled_atom='True means at least one reconstructed atom. False means both '
                    'atoms originate from observed atoms; their coordinates may have moved.',
                recovery_scope='The approved v2 policy permits observed atom motion under position '
                    'restraints. Failure remains a failed check, not permission to relax thresholds or retry.')
            diagnostic['observed_motion_acceptance'] = 'not_evaluated'
    # Recheck the receipt after reading its selected children.
    registered_output(root, folder, spec, result, 'pipeline.json', output['sources'][0]['sha256'])
    return output


def freeze_review(root, evaluation):
    raw = (json.dumps(evaluation, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()
    checksum = hashlib.sha256(raw).hexdigest()
    folder = root / 'state/receptor-reviews' / checksum[:32]
    if any(path.is_symlink() for path in (root / 'state', folder.parent, folder)):
        raise ValueError('Receptor review cannot traverse symbolic links')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'review.json'
    if not path.exists():
        ArtifactRegistry._write_json(path, evaluation)
    receptor_evidence.regular(root, path, checksum)
    return {'artifact_id': 'receptor-review-' + checksum[:32], 'manifest_sha256': checksum,
            'path': str(path.relative_to(root))}


def verify_review(root, reference, evaluation):
    checksum = reference['manifest_sha256']
    expected = 'state/receptor-reviews/' + checksum[:32] + '/review.json'
    if (reference['artifact_id'] != 'receptor-review-' + checksum[:32]
            or reference['path'] != expected):
        raise ValueError('Frozen receptor review identity differs')
    path, _ = receptor_evidence.regular(root, root / expected, checksum)
    if json.loads(path.read_text()) != evaluation:
        raise ValueError('Workflow receptor review differs from its immutable witness')


def evaluate(root, state, manager, spec, result, *, reuse=False):
    """Caller must first verify native approval and the current-study inputs."""
    if (result.state, result.return_code) != ('COMPLETED', 0):
        raise ValueError('Receptor review requires a completed preparation with exit code zero')
    handoff = receptor_context(state)
    record = handoff['receptor_preparation']
    previous = record.get('evaluation')
    witness = record.get('evaluation_artifact')
    if previous and witness:
        verify_review(root, witness, previous)
    implementation = sha256_file(Path(__file__))
    folder = manager.registry.job_dir(spec.run_id) / 'artifacts/receptor-preparation'
    manifest = registered_output(root, folder, spec, result, 'pipeline.json',
                                 previous['pipeline_sha256'] if previous else None)
    pipeline = json.loads(manifest.read_text())
    if spec.parameters.get('relaxation_policy_version') in {'v2', 'v2-local', 'v2-local-bounded', 'v2-local-block'}:
        proposal = evaluate_restrained(root, state, spec, result, folder, manifest, pipeline)
        record['proposal_evaluation_artifact'] = freeze_review(root, proposal)
        if record.get('motion_review'):
            from .ligand_receptor_admission import materialize
            from .ligand_receptor_input import validate_binding
            return materialize(root, state, record, proposal, validate_binding(root, state), folder)
        return proposal
    arguments = {'preparation_run_id': spec.run_id,
        'receptor_input_artifact_id': handoff['receptor_input']['artifact_id'],
        'acquisition_sha256': handoff['receptor_input']['manifest_sha256'],
        'prepared_sha256': pipeline.get('prepared_manifest_sha256')}
    evidence = receptor_evidence.inputs(root, **arguments)
    # Validate all pipeline files against the job's registered output receipt,
    # not merely against a mutable manifest supplied by the worker.
    for name, digest in pipeline['artifacts'].items():
        registered_output(root, folder, spec, result, name, digest)
    from .tool_api import library_first_assess_receptor_context_tool
    if previous:
        parent = previous['parents']
        receptor_evidence.regular(root, root / 'state/receptor_site_context' /
            parent['context_artifact_id'][17:] / 'site-context.json', parent['context_sha256'])
    context = library_first_assess_receptor_context_tool(root, {
        'receptor_input_artifact_id': arguments['receptor_input_artifact_id'],
        'manifest_sha256': arguments['acquisition_sha256'], 'preparation_run_id': spec.run_id,
        'prepared_receptor_manifest_sha256': arguments['prepared_sha256']})
    arguments.update(context_artifact_id=context['artifact_id'], context_sha256=context['manifest_sha256'])
    evidence = receptor_evidence.inputs(root, **arguments)
    if previous and (previous['parents'] != arguments or previous['result']['inventory'] != evidence['inventory']):
        raise ValueError('Previously verified receptor evidence changed; retain the original witness')
    if (reuse and previous and witness and previous['result']['sources'] == evidence['sources']
            and previous.get('review_implementation_sha256') == implementation):
        return previous
    review = receptor_evidence.replay(root, evidence)
    # Close the read/measure boundary before committing the review.
    current = receptor_evidence.inputs(root, **arguments)
    if current['inventory'] != evidence['inventory'] or current['sources'] != evidence['sources']:
        raise ValueError('Receptor evidence changed during review')
    registered_output(root, folder, spec, result, 'pipeline.json', sha256_file(manifest))
    evaluation = {'schema': 'ligand-receptor-review-v1', 'run_id': spec.run_id,
        'workflow_id': state['workflow_id'], 'design_id': state['design_id'],
        **({'vs_input_artifact_id': source_input(state)['artifact_id'],
            'target_artifact_id': source_input(state)['artifact_id']} if pre_vs(state) else
           {'candidate_artifact_id': handoff['artifact_id'], 'target_artifact_id': handoff['target']['artifact_id']}),
        'pipeline_sha256': sha256_file(manifest), 'parents': arguments, 'result': review,
        'review_implementation_sha256': implementation,
        'assessment_scope': 'isolated_receptor_quality_and_descriptive_context',
        'assessment': 'review_required', 'output_validation': 'RECEPTOR_QUALITY_AND_CONTEXT_REPLAYED',
        'receptor_prepared': True, 'prepared_scope': 'isolated_selected_chain_protein',
        'environment_suitability': 'not_established', 'covdock_ready': False,
        'md_ready': False, 'validated_inhibitor': False, 'compute_submitted_by_review': False}
    return json.loads(json.dumps(evaluation, allow_nan=False))


def evaluate_restrained(root, state, spec, result, folder, manifest, pipeline):
    """Replay registered restrained outputs; motion acceptance remains separate."""
    from .ligand_receptor_input import validate_binding
    from .receptor_quality import audit_restrained_receptor, observed_site_motion
    from .receptor_geometry import audit_restrained_geometry
    from .receptor_relaxation import RELAXATION_POLICY_V2
    handoff = receptor_context(state)
    selected_policy = spec.parameters.get('relaxation_policy')
    if spec.parameters.get('relaxation_policy_version') in {'v2-local', 'v2-local-bounded', 'v2-local-block'}:
        from .receptor_local_policy import validate_policy_version
        validate_policy_version(selected_policy, spec.parameters['relaxation_policy_version'])
    elif selected_policy != RELAXATION_POLICY_V2:
        raise ValueError('Unregistered restrained policy')
    if (pipeline.get('schema') != 'receptor-preparation-pipeline-v2'
            or pipeline.get('status') != 'observed_motion_review_pending'
            or pipeline.get('receptor_prepared') is not False
            or pipeline.get('target_acquisition_sha256') != handoff['receptor_input']['manifest_sha256']
            or pipeline.get('relaxation_policy') != selected_policy):
        raise ValueError('Restrained pipeline differs from the approved current-study proposal')
    manifest_sha = sha256_file(manifest)
    inventory = pipeline['artifacts']
    def verify_outputs():
        for name, digest in inventory.items():
            registered_output(root, folder, spec, result, name, digest)
        registered_output(root, folder, spec, result, 'pipeline.json', manifest_sha)
    verify_outputs()
    loaded = validate_binding(root, state)
    source = loaded['acquisition_directory'] / 'structure.pdb'
    relaxation, parent = folder / 'relaxation', folder / 'heavy'
    receipt_sha = inventory['relaxation/receipt.json']
    quality = audit_restrained_receptor(relaxation, receipt_sha, parent, source)
    quality_path = folder / 'identity-quality.json'
    if json.loads(json.dumps(quality)) != json.loads(quality_path.read_text()):
        raise ValueError('Registered restrained identity differs from independent replay')
    geometry = audit_restrained_geometry(relaxation, receipt_sha, quality_path,
        inventory['identity-quality.json'], source, parent)
    if json.loads(json.dumps(geometry)) != json.loads((folder / 'geometry-quality.json').read_text()):
        raise ValueError('Registered restrained geometry differs from independent replay')
    if geometry['status'] != 'geometry_passed_environment_review_pending':
        raise ValueError('Restrained geometry did not pass')
    mapping, = loaded['acquisition']['acquisition']['mappings']
    site_motion = observed_site_motion(quality['observed_coordinate_displacements'],
        chain=mapping['auth_asym_id'], author_residue=mapping['author_residue_id'])
    verify_outputs()
    validate_binding(root, state)
    return json.loads(json.dumps({
        'schema': 'ligand-receptor-review-v2', 'run_id': spec.run_id,
        'workflow_id': state['workflow_id'], 'design_id': state['design_id'],
        'pipeline_sha256': manifest_sha,
        'parents': {'preparation_run_id': spec.run_id,
            'acquisition_sha256': handoff['receptor_input']['manifest_sha256']},
        'result': {'status': 'observed_motion_review_pending', 'quality': quality,
            'geometry': geometry, 'site_motion': site_motion,
            'observed_motion_acceptance': 'not_evaluated'},
        'review_implementation_sha256': sha256_file(Path(__file__)),
        'assessment_scope': 'restrained_coordinate_proposal_identity_geometry_and_motion',
        'assessment': 'review_required', 'output_validation': 'OBSERVED_MOTION_REVIEW_PENDING',
        'receptor_prepared': False, 'prepared_scope': None,
        'environment_suitability': 'not_established', 'covdock_ready': False,
        'md_ready': False, 'validated_inhibitor': False, 'compute_submitted_by_review': False,
    }, allow_nan=False))


def verify_saved_readonly(root, state):
    """Verify a saved review and its dependencies without creating a new review."""
    from .execution import JobManager
    record = receptor_context(state)['receptor_preparation']
    previous = record['evaluation']
    verify_review(root, record['evaluation_artifact'], previous)
    if (previous['workflow_id'] != state['workflow_id'] or previous['design_id'] != state['design_id']
            or previous['run_id'] != record['run_id']
            or previous.get('review_implementation_sha256') != sha256_file(Path(__file__))):
        raise ValueError('Saved receptor review binding or implementation changed; evaluate_receptor is required')
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(record['run_id']), manager.registry.load_result(record['run_id'])
    if (result.state, result.return_code) != ('COMPLETED', 0):
        raise ValueError('Saved receptor review requires its completed preparation')
    folder = manager.registry.job_dir(spec.run_id) / 'artifacts/receptor-preparation'
    manifest = registered_output(root, folder, spec, result, 'pipeline.json', previous['pipeline_sha256'])
    pipeline = json.loads(manifest.read_text())
    for name, digest in pipeline['artifacts'].items():
        registered_output(root, folder, spec, result, name, digest)
    if previous['schema'] == 'ligand-receptor-review-v1':
        evidence = receptor_evidence.inputs(root, **previous['parents'])
        if (evidence['inventory'] != previous['result']['inventory']
                or evidence['sources'] != previous['result']['sources']):
            raise ValueError('Saved receptor evidence or audit implementation changed; evaluate_receptor is required')
    elif previous['schema'] == 'ligand-receptor-review-v2':
        proposal = evaluate_restrained(root, state, spec, result, folder, manifest, pipeline)
        witness = record['proposal_evaluation_artifact']
        verify_review(root, witness, proposal)
        if record.get('motion_review'):
            from .receptor_environment_review import load as load_review
            bound = {'workflow_id': state['workflow_id'], 'run_id': record['run_id'],
                'evaluation_sha256': witness['manifest_sha256']}
            judgment = load_review(root, record['motion_review'], bound,
                {witness['path']: witness['manifest_sha256']}, scope='restrained-receptor')
            admitted = judgment['decision'] == 'suitable_for_exploratory_receptor_preparation'
            if admitted != previous['receptor_prepared']:
                raise ValueError('Saved receptor admission differs from the current motion judgment')
        if previous['receptor_prepared']:
            if (previous.get('motion_review') != record.get('motion_review')
                    or previous.get('proposal_review') != witness):
                raise ValueError('Saved receptor admission differs from its bound proposal and judgment')
            for name, digest in previous['result']['inventory'].items():
                receptor_evidence.regular(root, root / name, digest)
        elif proposal != previous:
            raise ValueError('Saved receptor proposal differs from current registered evidence')
    else:
        raise ValueError('Unsupported saved receptor review schema')
    if record.get('report'):
        from .ligand_design import verify
        report = record['report']
        path = root / report['report_json']
        receptor_evidence.regular(root, path.parent / 'manifest.json', report['manifest_sha256'])
        verify(path.parent)
        payload = json.loads(path.read_text())
        if payload['evaluation'] != previous or payload['workflow_id'] != state['workflow_id']:
            raise ValueError('Saved receptor report differs from its review')
    return previous
