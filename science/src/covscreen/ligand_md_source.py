"""One active MD source, either current refinement or an explicit historical intake."""

def record(state):
    historical, current = state.get('historical_md_source'), state.get('covdock_execution')
    if historical and current:
        raise ValueError('A study cannot mix historical and current-refinement MD sources')
    if historical:
        return historical
    return current or {}


def project_id(state):
    return state['workflow_id'] if state.get('historical_md_source') else state['design_id']


def review_stage(state):
    return 'HISTORICAL_MD_SOURCE_REVIEW' if state.get('historical_md_source') else 'COVDOCK_EXECUTION_REVIEW'
