"""Select the real receptor source without inventing a refinement handoff."""
import hashlib
import json


VS_SOURCE = 'sealed-ligand-vs-receptor-v1'


def context(state):
    return state.get('refinement_input') or state.get('vs_receptor') or {}


def pre_vs(state):
    return not state.get('refinement_input') and bool(state.get('vs_input'))


def policy_replanning_available(state):
    return pre_vs(state) or (state.get('refinement_input') or {}).get('source_kind') == 'sealed-ligand-direct-lo-v1'


def source_input(state):
    return (state.get('vs_receptor') or {}).get('source_input') or state['vs_input']


def verify_inputs(root, state):
    if (state.get('refinement_input') or {}).get('source_kind') == 'sealed-ligand-direct-lo-v1':
        from .ligand_refinement_input import load_refinement_input
        from .ligand_refinement_target import load as load_target
        return load_refinement_input(root, state), load_target(root, state)
    if pre_vs(state):
        from .ligand_vs_input import load
        return load(root, {**state, 'vs_input': source_input(state)}, use_prepared=False)
    from .ligand_af3_execution import verify_inputs as verify_refinement
    return verify_refinement(root, state)


def planning_available(state):
    if pre_vs(state):
        from .ligand_vs_input import available
        return available(state) and not (context(state).get('receptor_preparation') or {}).get('run_id')
    from .ligand_receptor_preparation import STAGES
    return state['stage'] in STAGES and bool(context(state).get('target'))


def public_evaluation(evaluation):
    """Keep decisions and identity visible; full measurements have bound files."""
    output = {key: value for key, value in evaluation.items() if key != 'result'}
    result = evaluation.get('result') or {}
    output['result'] = {key: result[key] for key in ('status', 'identity', 'scope', 'receptor', 'limitations')
                        if key in result}
    for key in ('quality', 'geometry'):
        if key in result:
            output['result'][key] = {'status': result[key].get('status')}
    local = (result.get('quality') or {}).get('local_motion_audit')
    if local:
        output['result']['quality']['local_motion_audit'] = {k: local.get(k) for k in
            ('status', 'movable_observed_atom_count', 'fixed_observed_atom_count', 'policy_sha256')}
    if result.get('observed_motion_acceptance') == 'not_evaluated':
        output['result']['observed_motion_acceptance'] = 'not_evaluated'
        output['result']['receptor_prepared'] = False
    output['detail_scope'] = 'summary; full measurements are in the immutable evaluation artifact'
    return output


def public_record(record):
    output = {key: value for key, value in record.items() if key not in {'spec', 'approved_scope', 'evaluation'}}
    spec = record.get('spec')
    if spec:
        output['plan'] = {key: spec[key] for key in ('engine', 'route', 'project_id', 'request_id', 'resource_budget')
                          if key in spec}
        output['plan']['spec_sha256'] = hashlib.sha256(json.dumps(
            spec, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        parameters = spec.get('parameters') or {}
        output['plan']['execution'] = {key: parameters[key] for key in
            ('execution_instance', 'dispatch_policy', 'njobs', 'host', 'mode', 'reaction_type', 'input_row_count')
            if key in parameters}
        if parameters.get('relaxation_policy_version') in {'v2', 'v2-local', 'v2-local-bounded', 'v2-local-block'}:
            output['plan'].update(
                relaxation_policy_version=parameters['relaxation_policy_version'],
                relaxation_policy=parameters.get('relaxation_policy'),
                relaxation_policy_sha256=parameters.get('relaxation_policy_sha256'),
                observed_atoms_may_move=True,
                observed_motion_acceptance='not_evaluated',
                receptor_admission_after_computation=False)
    if record.get('evaluation'):
        output['evaluation'] = public_evaluation(record['evaluation'])
    return output


def public_context(value):
    output = {key: item for key, item in value.items()
              if key not in {'receptor_preparation', 'receptor_preparation_history'}}
    if value.get('receptor_preparation'):
        output['receptor_preparation'] = public_record(value['receptor_preparation'])
    if value.get('receptor_preparation_history'):
        output['receptor_preparation_history'] = [public_record(record)
            for record in value['receptor_preparation_history']]
    return output


def public_covdock_environment(result, handoff):
    """Expose navigable frozen evidence instead of repeating large raw nodes."""
    output = dict(result)
    reference = handoff.get('covdock_input') or {}
    requirements = result.get('review_requirements')
    if requirements and reference.get('path') and reference.get('manifest_sha256'):
        output['review_requirements'] = dict(requirements)
        for key in ('construct_audit', 'excluded_environment', 'site_context'):
            if key in requirements:
                output['review_requirements'][key] = {
                    'detail_scope': 'bound_evidence_reference; read before assessment',
                    'path': reference['path'], 'sha256': reference['manifest_sha256'],
                    'locator': '/review_requirements/' + key,
                }
    if 'evidence' in result:
        # read_evidence already returns either the discoverable document list or
        # one bounded, hash-verified node. The full lineage map duplicates it.
        output.pop('evidence_inventory', None)
        output['evidence_access'] = (
            'read_covdock_environment without evidence_path lists current bound documents. '
            'Read a document with evidence_path and evidence_locator; follow next_offset '
            'with evidence_offset until complete. References are not scientific judgments.')
    return output
