"""Verify stage explanations against actual gates, with unchanged captured evidence."""
import hashlib

import pytest

from covscreen.mcp.server import dispatch
from covscreen.workflow_protocol import begin_operation
from covscreen.workflow_store import load
from test_ligand_workflow import target_artifact


@pytest.mark.parametrize('route,entry,evidence,next_stage', [
    ('ligand', 'covalent_ligand_workflow', 'covalent_ligand_evidence', 'INTAKE'),
    ('library', 'covalent_library_workflow', 'covalent_library_research', 'STRUCTURE_REVIEW'),
])
def test_exploratory_selection_and_structural_assessment_are_separate_gates(tmp_path, target_artifact,
        route, entry, evidence, next_stage):
    initial = dispatch(tmp_path, entry, {'action':'start', 'request_id':'stage-context', 'target':'EGFR', 'taxon':9606})
    def call(current, tool, action, **args):
        return dispatch(tmp_path, tool, {'action':action, 'workflow_id':current['workflow']['workflow_id'],
            'expected_revision':current['workflow']['revision'], **args})
    assert not initial['stage_guidance']['select_site']['workflow_action_available']
    with pytest.raises(ValueError):
        call(initial, entry, 'select_site', site='C797', reason='Cannot select before verifying target identity')
    files=list((tmp_path/'state/target_identity').rglob('*'))
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
    verified=call(initial, evidence, 'resolve_target', **target_artifact)
    guidance=verified['stage_guidance']['select_site']
    assert guidance['workflow_action_available']
    assert not guidance['requires_structural_suitability_evidence_before_hypothesis']
    assert not guidance['establishes_reactivity']
    with pytest.raises(ValueError):
        call(verified, entry, 'select_site', site='C1', reason='A reason cannot turn a non-cysteine into a cysteine')
    state_dir=tmp_path/'state'/f'{route}-workflows'/verified['workflow']['workflow_id']
    selected=call(verified, entry, 'select_site', site='C797', reason='Explore a canonical cysteine; structural suitability remains unassessed')
    assert selected['workflow']['stage']==next_stage
    assert selected['site_selection']['reactivity_validated'] is False
    assert selected['execution']['state']=='not_submitted'
    assert 'stage_guidance' not in selected
    assert 'stage_guidance' not in load(state_dir)
    assert not (tmp_path/'state/target_structures').exists()
    assert {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}==before
    # Selection opens further research; it cannot skip to generation or receptor execution.
    if route=='ligand':
        with pytest.raises(ValueError):
            call(selected,'covalent_ligand_design_step','generate')
    else:
        with pytest.raises(ValueError):
            call(selected,'covalent_library_receptor','start')


@pytest.mark.parametrize('route,entry,evidence', [
    ('ligand','covalent_ligand_workflow','covalent_ligand_evidence'),
    ('library','covalent_library_workflow','covalent_library_research'),
])
def test_pending_recovery_hides_site_selection_despite_existing_identity(tmp_path, target_artifact,route,entry,evidence):
    start=dispatch(tmp_path,entry,{'action':'start','request_id':'pending-stage','target':'EGFR','taxon':9606})
    verified=dispatch(tmp_path,evidence,{'action':'resolve_target','workflow_id':start['workflow']['workflow_id'],
        'expected_revision':start['workflow']['revision'],**target_artifact})
    out=tmp_path/'state'/f'{route}-workflows'/verified['workflow']['workflow_id']
    begin_operation(out,load(out),evidence,'resolve_target')
    observed=dispatch(tmp_path,entry,{'action':'status','workflow_id':verified['workflow']['workflow_id']})
    assert not observed['stage_guidance']['select_site']['workflow_action_available']
    assert 'select_site' not in observed['workflow']['tools'][entry]['actions']
    assert 'reconcile' in observed['workflow']['tools'][entry]['actions']
    assert observed['execution']['state']=='not_submitted'
    assert observed['execution']['task_ids']==[]
    before=(out/'state.json').read_bytes()
    with pytest.raises(ValueError):
        dispatch(tmp_path,entry,{'action':'select_site','workflow_id':verified['workflow']['workflow_id'],
            'expected_revision':verified['workflow']['revision'],'site':'C797',
            'reason':'Existing identity cannot bypass an unresolved operation'})
    assert (out/'state.json').read_bytes()==before
