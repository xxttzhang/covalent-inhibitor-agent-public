"""Real MCP, approval and state protocol; artificial CovDock data and SDK only.

Historical 1M17 is read by the real receptor parser. Upstream eligibility is a
controlled test decision, not admission of that construct in production.
"""
import json

import pytest

from covscreen import refinement_output
from covscreen.covdock_readback import CovDockReadbackUnavailable
from covscreen.ligand_workflow import DESIGN, navigate
from covscreen.mcp.server import dispatch
from covdock_readback_fixture import outputs
from test_covdock_execution_workflow import (ready, approval, design, graph_design, pose,
    bound, planned_receptor, isolated, completed, reviewed, args, jobs)
from test_af3_evidence_workflow import inputs


def test_three_candidate_partial_readback_report_resume_and_runtime_recovery(ready, monkeypatch):
    case, launches, _, manager, _ = ready
    response = dispatch(case[0], DESIGN, approval(case))
    run = response['covdock_execution']['run_id']
    spec = manager.registry.load_spec(run)
    assert spec.parameters['refinement_input_contract']['input_count'] == 3
    assert spec.parameters['covdock_readback']['runtime']['attachment_residue'] == 'A:773'
    assert spec.parameters['covdock_readback']['runtime']['runtime_lib_dir'] is None
    _, sdk = outputs(case[0], manager, spec, missing_strain=True)
    monkeypatch.setattr(refinement_output, '_covdock_sdk', sdk)
    response = dispatch(case[0], DESIGN, args(case, 'evaluate_covdock'))
    review = response['covdock_execution']['evaluation']
    assert review['candidate_count'] == 3 and review['measured_candidate_count'] == 2
    assert review['failed_candidate_count'] == 1
    assert [row['gate']['physical_validity'] for row in review['candidates']] == ['UNKNOWN', 'PASS', 'UNKNOWN']
    assert all(row['gate']['cross_method_consensus'] == 'NA' for row in review['candidates'])
    assert not any(row['gate']['review_eligible'] for row in review['candidates'])
    assert not review['scientific_result_verified'] and not review['md_ready']
    response = dispatch(case[0], DESIGN, args(case, 'report_covdock'))
    report = response['covdock_execution']['report']
    assert report['candidate_count'] == 3 and not report['compute_submitted_by_report']
    assert 'prepare_md_handoff' in response['workflow']['tools'][DESIGN]['actions']
    before = jobs(case)
    response = dispatch(case[0], DESIGN, args(case, 'prepare_md_handoff'))
    delivery = response['covdock_execution']['md_handoff']
    assert response['md_admission']['state'] == 'not_evaluated'
    assert delivery['candidate_count'] == 3 and delivery['task_id'] == run
    assert delivery['md_admission']['state'] == 'not_evaluated'
    assert delivery['md_admission']['rejection_reasons'] == []
    assert not delivery['compute_submitted_by_handoff'] and jobs(case) == before
    assert all(row['state'] == 'ineligible_for_pose_selection' for row in delivery['pose_selection_assessment'])
    assert any(item['kind'] == 'shared_md_candidate_delivery' for item in response['artifacts'])
    assert dispatch(case[0], DESIGN, args(case, 'prepare_md_handoff'))['covdock_execution']['md_handoff'] == delivery
    def unavailable(*args):
        raise CovDockReadbackUnavailable('Controlled unavailable SDK; not molecular invalidity')
    monkeypatch.setattr(refinement_output, '_covdock_sdk', unavailable)
    resumed = navigate(case[0], 'resume', workflow_id=case[1])
    assert resumed['covdock_execution']['report'] == report
    assert resumed['covdock_execution']['md_handoff'] == delivery
    path = case[0] / delivery['path']
    original = path.read_bytes()
    path.write_text('{}')
    rejected_handoff = navigate(case[0], 'status', workflow_id=case[1])
    assert 'md_handoff' not in rejected_handoff['covdock_execution']
    assert rejected_handoff['covdock_execution']['md_handoff_error']
    assert rejected_handoff['covdock_execution']['evaluation_verification'] == 'verified'
    assert rejected_handoff['covdock_execution']['report'] == report
    path.write_bytes(original)
    assert dispatch(case[0], DESIGN, args(case, 'prepare_md_handoff'))['covdock_execution']['md_handoff'] == delivery
    assert dispatch(case[0], DESIGN, args(case, 'report_covdock'))['covdock_execution']['report'] == report
    with pytest.raises(CovDockReadbackUnavailable):
        dispatch(case[0], DESIGN, args(case, 'evaluate_covdock'))
    saved = json.loads(case[2].read_text())['covdock_execution']
    assert saved['run_id'] == run and saved['evaluation_verification'] == 'verification_unavailable'
    assert 'report' not in saved and 'evidence_error' not in saved
    monkeypatch.setattr(refinement_output, '_covdock_sdk', sdk)
    dispatch(case[0], DESIGN, args(case, 'evaluate_covdock'))
    assert dispatch(case[0], DESIGN, args(case, 'report_covdock'))['covdock_execution']['report'] == report
    (case[0] / report['report_markdown']).write_text('Tampered software fixture report')
    with pytest.raises(ValueError):
        dispatch(case[0], DESIGN, args(case, 'report_covdock'))
    rejected = navigate(case[0], 'status', workflow_id=case[1])
    assert rejected['covdock_execution']['run_id'] == run
    assert 'report' not in rejected['covdock_execution']
    assert len(launches) == 2 and len(jobs(case)) == 3
