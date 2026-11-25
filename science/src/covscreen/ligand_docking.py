"""Choose and freeze a source-bound docking route before screening a batch."""
from .ligand_candidate_source import read_design
from .ligand_docking_policy import decide
from .ligand_parent_evidence import version_lock
from .workflow_protocol import changed, revision
from .workflow_store import load


def verified_decision(state, candidate_count):
    saved = state.get('docking_decision')
    if not saved:
        return decide(candidate_count)
    expected = decide(candidate_count, requested_route=saved['requested_route'],
                      reason=saved['selection_reason'])
    if expected != saved:
        raise ValueError('Docking route differs from the verified complete candidate batch')
    return expected


def prepare(root, *, workflow_id, expected_revision, docking_route='auto', reason=None, **arguments):
    from .ligand_direct_lo import available, freeze as freeze_lo, STAGE
    from .ligand_vs_input import freeze as freeze_vs
    from .ligand_workflow import directory, response
    location = directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, expected_revision)
        if arguments or not available(state):
            raise ValueError('Docking routing requires an idle design without frozen docking inputs')
        source = read_design(root, state)
        decision = decide(len(source['admitted_indices']), requested_route=docking_route, reason=reason)
        if decision['selected_route'] == 'no_candidates':
            raise ValueError('No chemically admitted candidates; no docking input created')
        state['docking_decision'] = decision
        if decision['selected_route'] == 'direct_lo':
            state['refinement_input'] = freeze_lo(root, state)
            state['stage'] = STAGE
        else:
            state['vs_input'] = freeze_vs(root, state)
        changed(location, state, 'prepare_docking_input')
        return response(state, {'docking_decision': decision,
            'compute_submitted': False, 'execution_authorized': False})
