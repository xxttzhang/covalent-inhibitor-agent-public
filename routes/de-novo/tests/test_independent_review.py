"""Review/approval boundaries over artificial metadata; no scientific success claimed."""
import hashlib
import json
from pathlib import Path

import pytest
from covscreen.approval_scope import normalize_spec
from covscreen.jobs import JobSpec
from covscreen.native_approval import NativeApprovalDenied, verify_spec
from covscreen_de_novo.independent_review import (assessment, encoded, folder_for,
    read_plan, requirements, source, validate_compiled_spec)


def plan_spec():
    digest = 'a'*64
    return JobSpec(run_id='artificial-run-000001', request_id='artificial-request', project_id='artificial-project',
        route='de-novo', engine='schrodinger-covdock-lo', command=['not-executed'], working_directory='/fixture',
        output_directory='/fixture/output', parameters={'denovo_source_id': 'denovo-review-'+digest[:32],
        'denovo_source_manifest_sha256': digest}, resource_budget={'gpu_count': 0, 'cpu_count': 1,
        'memory_gb': 8, 'max_runtime_minutes': 30, 'max_candidates': 2})


def save_plan(root, spec):
    identity = spec.parameters['denovo_source_id'];digest = spec.parameters['denovo_source_manifest_sha256']
    folder = folder_for(root, identity);folder.mkdir(parents=True)
    normalized = normalize_spec(spec)
    value = {'schema':'native-denovo-independent-plan-v1','source_id':identity,'source_manifest_sha256':digest,
        'spec':normalized,'spec_sha256':hashlib.sha256(encoded(normalized)).hexdigest()}
    (folder/'plan.json').write_bytes(encoded(value))
    return identity, digest


def test_execution_must_match_frozen_plan_but_approval_metadata_is_separate(tmp_path):
    spec = plan_spec();identity, digest = save_plan(tmp_path, spec)
    execution = spec.model_copy(update={'execution_mode':'execute','human_approved':True,
        'approval_note':'Artificial software fixture only'})
    validate_compiled_spec(tmp_path, execution)
    with pytest.raises(NativeApprovalDenied, match='no native approval'):
        verify_spec(execution)
    changed = execution.model_copy(deep=True)
    changed.resource_budget.max_runtime_minutes = 31
    with pytest.raises(ValueError, match='changed after plan freeze'):
        validate_compiled_spec(tmp_path, changed)
    changed = execution.model_copy(deep=True)
    changed.command.append('--unexpected')
    with pytest.raises(ValueError, match='changed after plan freeze'):
        validate_compiled_spec(tmp_path, changed)
    with pytest.raises(ValueError, match='identity changed'):
        read_plan(tmp_path, identity, 'b'*64)


def test_source_path_is_content_addressed_and_not_model_selected(tmp_path):
    with pytest.raises(ValueError, match='identity differs'):
        source(tmp_path, 'denovo-review-'+'a'*32, 'b'*64)
    with pytest.raises(ValueError, match='identity required'):
        source(tmp_path, '../other-study', 'a'*64)
    folder = tmp_path/'state/de-novo-inputs';folder.mkdir(parents=True)
    (folder/('denovo-review-'+'a'*32)).symlink_to(tmp_path)
    with pytest.raises(ValueError, match='symlinks'):
        source(tmp_path, 'denovo-review-'+'a'*32, 'a'*64)


def test_source_verification_requires_an_explicit_scientific_runtime(tmp_path):
    with pytest.raises(ValueError, match='Register the independent'):
        source(tmp_path, 'denovo-review-'+'a'*32, 'a'*64)


@pytest.mark.parametrize('engine,physical,complete,expected', [
    ('FAILED', 'UNKNOWN', False, 'ENGINE_FAILED'),
    ('SUCCESS', 'FAIL', True, 'PHYSICAL_GATE_REJECTED'),
    ('SUCCESS', 'UNKNOWN', False, 'EVIDENCE_INSUFFICIENT'),
    ('SUCCESS', 'PASS', False, 'EVIDENCE_INSUFFICIENT'),
    ('SUCCESS', 'PASS', True, 'ADDUCT_PHYSICAL_CHECKS_PASSED_SCIENTIFIC_REVIEW_PENDING')])
def test_evaluation_never_promotes_physical_checks_to_full_scientific_handoff(engine, physical, complete, expected):
    review = {'candidates': [{'input_sdf_index':0,'compound_id':'candidate_000','engine_status':engine,
        'gate':{'physical_validity':physical,'physical_evidence_complete':complete}}]}
    assert assessment(review) == expected
    limits = requirements()
    assert limits['independent_prereaction_evaluation'] == 'NOT_PERFORMED'
    assert limits['control_redocking_validation'] == 'NOT_PERFORMED'
    assert not limits['ready_for_scientific_handoff']
    assert limits['native_md_admission'] == 'NOT_PERFORMED'


def test_missing_selected_candidate_never_adopts_a_control():
    with pytest.raises(ValueError, match='lost the frozen candidate'):
        assessment({'candidates':[{'input_sdf_index':1,'compound_id':'control_afatinib'}]})


def test_generation_census_over_256_files_keeps_full_manifest_and_bounded_job_lineage(tmp_path, monkeypatch):
    from covscreen_de_novo import independent_review as adapter
    required = ['generation/request.json', 'generation/authorization.json', 'generation/status.json',
        'independent/input_manifest.json', 'independent/candidate_000.sdf', 'independent/protein_A.pdb',
        'control/control_manifest.json', 'control/control_afatinib.sdf',
        'control/control_crystal_adduct.sdf', 'control/control_atom_map.json', 'candidates.csv']
    files = {name:'a'*64 for name in required}
    for slot in range(96):
        files.update({f'generation/{kind}/{slot}.sdf':'b'*64 for kind in ['download', 'acceptance', 'relaxed']})
    value = {'study_id':'artificial-native-study','project_id':'artificial-project', 'files':files,
        'receptor_sha256':'c'*64, 'input_contract':{'input_count':2,'input_sha256':'d'*64,'identity_sha256':'e'*64}}
    # Only source verification is stubbed: this tests the actual request compiler
    # against a large artificial census, not chemical validation or job execution.
    source_id = 'denovo-review-'+'a'*32
    from covscreen_de_novo.covdock_input import REGISTRY
    receptor = tmp_path/'state/de-novo-inputs'/source_id/'independent/protein_A.pdb'
    receptor.parent.mkdir(parents=True)
    receptor.write_bytes((REGISTRY/'assets/protein_A.pdb').read_bytes())
    def verified_source(*args, validation_release=None):
        assert validation_release is None  # A fresh plan must use the current release.
        return tmp_path/'state/de-novo-inputs'/source_id, value
    monkeypatch.setattr(adapter, 'source', verified_source)
    request = adapter.request_for(tmp_path, source_id, 'a'*64)
    assert len(value['files']) == 299
    assert len(request.parameters['refinement_lineage_files']) == 14
    assert request.parameters['refinement_lineage_files'][0].endswith('/source.json')
    assert request.parameters['expected_input_count'] == 2
    assert request.resource_budget.max_candidates == 2
    assert request.execution_mode == 'plan' and not request.human_approved


def test_status_keeps_prior_evaluation_and_marks_it_not_recomputed(tmp_path, monkeypatch):
    from covscreen_de_novo import independent_review as adapter
    source_id = 'denovo-review-'+'a'*32
    folder = adapter.folder_for(tmp_path, source_id);(folder/'observations').mkdir(parents=True)
    prior = {'study_id':'artificial-study','source_id':source_id,'source_manifest_sha256':'a'*64,
        'run_id':'artificial-run','adduct_review':{'fixture':'metadata only; not science'},
        'independent_adduct_evaluation':'EVIDENCE_INSUFFICIENT',
        'control_redocking_review':{'fixture':'metadata only; not science'},
        'control_redocking_validation':'PASS'}
    raw = adapter.encoded(prior);digest=hashlib.sha256(raw).hexdigest()
    path=folder/'observations'/(digest+'.json');path.write_bytes(raw)
    (folder/'latest.json').write_bytes(adapter.encoded({'path':str(path.relative_to(tmp_path)), 'sha256':digest}))
    monkeypatch.setattr(adapter, 'observe', lambda *args: ({'study_id':'artificial-study',
        'source_id':source_id,'source_manifest_sha256':'a'*64,'run_id':'artificial-run',
        'independent_adduct_evaluation':'NOT_PERFORMED'}, None))
    first=adapter.step(tmp_path,'status',source_id,'a'*64)
    second=adapter.step(tmp_path,'status',source_id,'a'*64)
    assert first['independent_adduct_evaluation'] == second['independent_adduct_evaluation'] == 'EVIDENCE_INSUFFICIENT'
    assert first['evaluation_receipt_recomputed_by_status'] is False
    assert second['last_adduct_review'] == prior['adduct_review']
    assert second['control_redocking_review'] == prior['control_redocking_review']
    assert second['control_redocking_validation'] == 'PASS'
    assert path.read_bytes() == raw


@pytest.mark.parametrize('control_state', ['PASS', 'FAIL', 'EVIDENCE_INSUFFICIENT'])
@pytest.mark.parametrize('chemical_state', ['PASS', 'FAIL', 'EVIDENCE_INSUFFICIENT'])
def test_evaluation_projects_control_and_never_bypasses_remaining_md_gates(tmp_path, monkeypatch, control_state, chemical_state):
    from types import SimpleNamespace
    from covscreen_de_novo import independent_review as adapter, control_review, chemical_review
    source_id = 'denovo-review-'+'a'*32
    spec = SimpleNamespace(run_id='artificial-run')
    job = SimpleNamespace(state='COMPLETED', return_code=0)
    def observe(*args):
        return ({'study_id':'artificial-study','source_id':source_id,'source_manifest_sha256':'a'*64,
            'run_id':spec.run_id, 'state':'COMPLETED','independent_adduct_evaluation':'NOT_PERFORMED',
            **adapter.requirements()}, (None,spec,job))
    monkeypatch.setattr(adapter, 'observe', observe)
    monkeypatch.setattr(adapter, 'source', lambda *args:(tmp_path, {}))
    monkeypatch.setattr(adapter, 'evaluate_outputs', lambda *args,**kwargs:{'candidates':[
        {'input_sdf_index':0,'compound_id':'candidate_000','engine_status':'SUCCESS',
         'gate':{'physical_validity':'PASS','physical_evidence_complete':True}}]})
    monkeypatch.setattr(control_review, 'evaluate', lambda *args:{'state':control_state,
        'fixture':'artificial gate projection; not scientific data','md_ready':False})
    monkeypatch.setattr(chemical_review, 'evaluate', lambda *args:{'state':chemical_state,
        'fixture':'artificial gate projection; not scientific data','md_ready':False})
    result = adapter.step(tmp_path,'prepare_md_handoff',source_id,'a'*64)
    assert result['control_redocking_validation'] == control_state
    assert result['full_chemical_graph_and_stereochemistry_review'] == chemical_state
    assert result['state'] == 'MD_HANDOFF_BLOCKED'
    assert result['md_handoff']['candidate_delivery'] is None
    assert result['independent_prereaction_evaluation'] == 'NOT_PERFORMED'
    assert not result['ready_for_scientific_handoff']
    status = adapter.step(tmp_path,'status',source_id,'a'*64)
    assert status['control_redocking_validation'] == control_state
    assert status['full_chemical_graph_and_stereochemistry_review'] == chemical_state
    assert status['evaluation_receipt_recomputed_by_status'] is False


def test_control_readback_failure_is_persisted_as_missing_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from covscreen_de_novo import independent_review as adapter, control_review
    source_id = 'denovo-review-'+'a'*32
    monkeypatch.setattr(adapter, 'observe', lambda *args: ({'study_id':'artificial-study',
        'source_id':source_id, 'source_manifest_sha256':'a'*64, 'run_id':'artificial-run',
        'state':'COMPLETED', **adapter.requirements()},
        (None,None,SimpleNamespace(state='COMPLETED',return_code=0))))
    monkeypatch.setattr(adapter, 'source', lambda *args:(tmp_path, {}))
    monkeypatch.setattr(adapter, 'evaluate_outputs', lambda *args,**kwargs:{'candidates':[
        {'input_sdf_index':0,'compound_id':'candidate_000','engine_status':'SUCCESS',
         'gate':{'physical_validity':'PASS','physical_evidence_complete':True}}]})
    def unavailable(*args): raise ValueError('artificial missing vendor runtime')
    monkeypatch.setattr(control_review, 'evaluate', unavailable)
    first = adapter.step(tmp_path,'evaluate',source_id,'a'*64)
    status = adapter.step(tmp_path,'status',source_id,'a'*64)
    assert first['state'] == 'EVIDENCE_REVIEW_REQUIRED'
    assert status['control_redocking_validation'] == 'EVIDENCE_INSUFFICIENT'
    assert status['control_redocking_review']['md_ready'] is False
