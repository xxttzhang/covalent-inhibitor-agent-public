"""Attach a preserved historical source to an empty study, without adopting jobs."""
from .md_historical_source import load_source, SCHEMA
from .workflow_protocol import changed, revision
from .workflow_store import load
from .ligand_parent_evidence import version_lock


def refresh(root, state):
    record = state.get('historical_md_source')
    if not record:
        return
    try:
        loaded = load_source(root, record['artifact_id'], record['manifest_sha256'])
        if (loaded['manifest']['schema'] != SCHEMA or loaded['manifest']['identity'] != record['identity']
                or record['requested_scope'] != state['target']):
            raise ValueError('Historical source identity or requested study scope changed')
    except (ValueError, KeyError, TypeError, OSError) as error:
        record.update(verification='evidence_rejected', evidence_error=str(error))
    else:
        record['verification'] = 'source_chain_verified'
        record.pop('evidence_error', None)


def attach(root, *, workflow_id, expected_revision, source_artifact_id, source_manifest_sha256, reason):
    from .ligand_workflow import directory, response
    folder = directory(root, workflow_id)
    with version_lock(folder, '.workflow.lock'):
        state = load(folder)
        revision(state, expected_revision)
        if (state.get('pending') or state['stage'] not in {'INTAKE', 'HISTORICAL_MD_SOURCE_REVIEW'}
                or any(state.get(key) for key in ('evidence_id', 'design_id', 'pose_execution',
                    'af3_execution', 'covdock_execution', 'md_graph_execution', 'md_parameterization'))):
            raise ValueError('Historical source attachment requires an empty idle study')
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 2000:
            raise ValueError('Record why this historical candidate is being continued')
        loaded = load_source(root, source_artifact_id, source_manifest_sha256)
        if loaded['manifest']['schema'] != SCHEMA:
            raise ValueError('Ligand research requires a historical Ligand source')
        record = {'artifact_id': source_artifact_id, 'manifest_sha256': source_manifest_sha256,
            'identity': loaded['manifest']['identity'], 'requested_scope': dict(state['target']),
            'reason': reason.strip(), 'verification': 'source_chain_verified',
            'canonical_scope_verified': False, 'native_graph_qc_completed': False,
            'parameterization_completed': False, 'calculation_submitted': False,
            'limitations': list(loaded['manifest']['limitations'])}
        previous = state.get('historical_md_source')
        if previous:
            refresh(root, state)
            if state['historical_md_source'] != record:
                raise ValueError('Reconsider the unexecuted historical source before changing it')
            return response(state)
        state['historical_md_source'] = record
        state['stage'] = 'HISTORICAL_MD_SOURCE_REVIEW'
        changed(folder, state, 'attach_md_source', {'artifact_id': source_artifact_id,
            'manifest_sha256': source_manifest_sha256, 'reason': reason.strip()})
        return response(state)


def validate_selection(root, state):
    from .ligand_workflow import read_target
    from .md_candidate_pose import load_candidate_pose_input
    refresh(root, state)
    record = state['historical_md_source']
    if record.get('verification') != 'source_chain_verified':
        raise ValueError(record.get('evidence_error', 'Historical source is not verified'))
    selection = record['md_selection']
    target = read_target(root, state, selection['target_reference'])
    loaded = load_candidate_pose_input(root, selection['artifact_id'], selection['manifest_sha256'])
    value = loaded['manifest']
    if (value.get('schema') != 'historical-candidate-pose-input-v1'
            or selection['source_manifest_sha256'] != record['manifest_sha256']
            or value['parents']['historical_source_sha256'] != record['manifest_sha256']
            or value['parents']['target_identity_sha256'] != selection['target_reference']['target_manifest_sha256']
            or value['target']['uniprot'] != target['identity']['unique_verified_target']
            or 'C' + str(value['target']['canonical_cys']) != state['target']['site']
            or any(value[k] != selection[k] or selection[k] != record['identity'][k]
                   for k in ('compound_id', 'input_sdf_index'))
            or value['candidate_id'] != selection['candidate_id']
            or record.get('md_handoff') != {'artifact_id': record['artifact_id'],
                'manifest_sha256': record['manifest_sha256'], 'target_reference': selection['target_reference']}):
        raise ValueError('Historical selected pose differs from its study, source or target identity')
    return loaded


def prepare_candidate(root, *, workflow_id, expected_revision, target_artifact_id, target_manifest_sha256, reason):
    from .ligand_workflow import directory, response, read_target
    from .md_historical_pose import freeze
    from .ligand_md_selection import refresh as refresh_selection
    folder = directory(root, workflow_id)
    with version_lock(folder, '.workflow.lock'):
        state = load(folder)
        revision(state, expected_revision)
        if state.get('pending') or state.get('md_graph_execution') or state['stage'] != 'HISTORICAL_MD_SOURCE_REVIEW':
            raise ValueError('Prepare only an unexecuted historical source; observe any original graph task')
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 2000:
            raise ValueError('Record a reason for selecting this historical candidate')
        refresh(root, state)
        record = state['historical_md_source']
        if record.get('verification') != 'source_chain_verified':
            changed(folder, state, 'historical_source_rejected')
            raise ValueError(record['evidence_error'])
        reference = {'target_artifact_id': target_artifact_id, 'target_manifest_sha256': target_manifest_sha256}
        read_target(root, state, reference)
        if record.get('md_selection'):
            previous = record['md_selection']
            if previous['target_reference'] != reference or previous['reason'] != reason.strip():
                raise ValueError('Release the existing candidate selection before changing it')
            validate_selection(root, state)
            return response(state)
        selected = freeze(root, record, reference, int(state['target']['site'][1:]))
        record['md_selection'] = {**selected, 'reason': reason.strip(),
            'decision_scope': 'historical_candidate_selection_native_graph_pending'}
        record['md_handoff'] = {'artifact_id': record['artifact_id'], 'manifest_sha256': record['manifest_sha256'],
                               'target_reference': reference}
        validate_selection(root, state)
        refresh_selection(root, state)
        changed(folder, state, 'prepare_historical_md_candidate')
        return response(state)
