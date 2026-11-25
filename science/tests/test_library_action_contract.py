"""Prevent the MCP vocabulary and stage field projection from drifting apart."""
from covscreen.mcp.library_workflow import TOOLS
from covscreen_library_first.workflow_actions import FIELDS, RESEARCH_ACTIONS, RECEPTOR_ACTIONS
from covscreen_library_first.workflow import RESEARCH, RECEPTOR, view


def test_published_actions_have_exact_backend_fields():
    assert set(RESEARCH_ACTIONS).isdisjoint(RECEPTOR_ACTIONS)
    assert set(RESEARCH_ACTIONS) | set(RECEPTOR_ACTIONS) == set(FIELDS)
    for name, actions in ((RESEARCH, RESEARCH_ACTIONS), (RECEPTOR, RECEPTOR_ACTIONS)):
        schema = next(t['inputSchema'] for t in TOOLS if t['name'] == name)
        assert schema['properties']['action']['enum'] == list(actions)
        for action in actions:
            assert set(FIELDS[action]) <= set(schema['properties']), action


def test_full_vocabulary_does_not_expose_later_actions_in_target_stage():
    state = {'stage':'TARGET_REVIEW', 'workflow_id':'library-flow-'+'a'*24,
             'revision':1, 'pending':None, 'branches':[], 'target_identity':None}
    tools = view(state)['tools']
    assert tools[RESEARCH]['actions'] == ['resolve_target']
    assert RECEPTOR not in tools
    assert 'approve_screening' not in tools[RESEARCH]['actions']
    assert 'start_md_admission' not in tools[RESEARCH]['actions']
