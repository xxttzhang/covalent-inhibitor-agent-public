"""Explicit historical input branch; old executions never become current jobs."""
from copy import deepcopy
from uuid import uuid4

from covscreen.md_historical_source import load_source
from covscreen.md_historical_library_source import SCHEMA
from covscreen.md_historical_pose import freeze
from covscreen.md_candidate_pose import load_candidate_pose_input

ACTIONS = {'attach_historical_md_source', 'read_historical_md_source', 'prepare_historical_md_candidate'}


def validate(root, state, *, candidate=False):
    from .workflow import identity
    record = state['historical_md_source']
    loaded = load_source(root, record['artifact_id'], record['manifest_sha256'])
    if (loaded['manifest']['schema'] != SCHEMA or loaded['manifest']['identity'] != record['identity']
            or record['target'] != state['target'] or record['site_selection'] != state['site_selection']
            or record['target_identity'] != state['target_identity']):
        raise ValueError('Historical Library source differs from the study target, site or source identity')
    target = identity(root, state)
    if candidate:
        asset, choice = state['md_input'], state['md_candidate']
        pose = load_candidate_pose_input(root, asset['artifact_id'], asset['manifest_sha256'])
        value = pose['manifest']
        if (value['origin']['route'] != 'library-first' or value['schema'] != 'historical-candidate-pose-input-v1'
                or value['parents']['historical_source_sha256'] != record['manifest_sha256']
                or value['parents']['target_identity_sha256'] != state['target_identity']['sha256']
                or value['target']['uniprot'] != target['identity']['unique_verified_target']
                or 'C'+str(value['target']['canonical_cys']) != state['site_selection']['site']
                or asset['choice_id'] != choice['id'] or choice['source_manifest_sha256'] != record['manifest_sha256']
                or any(value[k] != record['identity'][k] or value[k] != choice['candidate'][k]
                       for k in ('compound_id', 'input_sdf_index'))
                or value['candidate_id'] != asset['candidate_id']):
            raise ValueError('Historical Library pose differs from the selected candidate and canonical target')
        return pose
    return loaded


def refresh(root, state):
    record = state.get('historical_md_source')
    if not record:
        return
    try:
        validate(root, state, candidate=bool(state.get('md_input')))
    except (ValueError, KeyError, TypeError, OSError) as error:
        record.update(verification='evidence_rejected', error=str(error))
    else:
        record['verification'] = 'source_chain_verified'
        record.pop('error', None)


def execute(root, state, action, args):
    from .workflow import ASSETS, identity
    if action == 'attach_historical_md_source':
        if (state['stage'] != 'STRUCTURE_REVIEW' or state.get('historical_md_source')
                or any(state.get(k) for k in (*ASSETS[2:], 'screening_handoff', 'refinement_handoff',
                                              'md_candidate', 'md_input', 'md_execution'))):
            raise ValueError('Attach historical input only after target/site selection in an empty study')
        reason = args['reason']
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 2000:
            raise ValueError('A concrete reason is required for historical intake')
        identity(root, state)
        loaded = load_source(root, args['source_artifact_id'], args['source_manifest_sha256'])
        if loaded['manifest']['schema'] != SCHEMA:
            raise ValueError('Library research requires a historical Library source')
        state['historical_md_source'] = {
            'artifact_id': args['source_artifact_id'], 'manifest_sha256': args['source_manifest_sha256'],
            'identity': loaded['manifest']['identity'], 'target': deepcopy(state['target']),
            'site_selection': deepcopy(state['site_selection']), 'target_identity': deepcopy(state['target_identity']),
            'reason': reason.strip(), 'verification': 'source_chain_verified',
            'canonical_scope_verified': False, 'native_graph_qc_completed': False,
            'calculation_submitted': False, 'limitations': loaded['manifest']['limitations']}
        state['stage'] = 'HISTORICAL_MD_SOURCE_REVIEW'
    elif action == 'read_historical_md_source':
        refresh(root, state)
    elif action == 'prepare_historical_md_candidate':
        validate(root, state)
        if state.get('md_input') or state.get('md_execution') or state['stage'] != 'HISTORICAL_MD_SOURCE_REVIEW':
            raise ValueError('Prepare only an unexecuted historical input')
        record = state['historical_md_source']
        target = {'target_artifact_id': state['target_identity']['id'],
                  'target_manifest_sha256': state['target_identity']['sha256']}
        selected = freeze(root, record, target, int(state['site_selection']['site'][1:]))
        choice_id = 'md-choice-'+uuid4().hex
        state['md_candidate'] = {'schema': 'library-historical-md-choice-v1', 'id': choice_id,
            'candidate': {k: selected[k] for k in ('compound_id', 'input_sdf_index')},
            'source_manifest_sha256': record['manifest_sha256'], 'reason': record['reason']}
        state['md_input'] = {**selected, 'choice_id': choice_id}
        validate(root, state, candidate=True)
        record['canonical_scope_verified'] = True
    else:
        raise ValueError('Unsupported historical input action')
    return deepcopy(state['historical_md_source'])
