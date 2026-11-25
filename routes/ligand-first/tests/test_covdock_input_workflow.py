"""Actual shared planner; upstream scientific decisions are controlled fixtures.

Reuses historical 1M17 readback from test_receptor_evidence_workflow. Its real
unmapped construct remains blocked in production. Vendor files are inert
placeholders, never invoked; no license checkout or CovDock computation occurs.
"""
import json

import pytest

from covscreen.execution import JobManager
from covscreen.jobs import sha256_file
from covscreen.ligand_covdock_input import load, request_for
from covscreen.ligand_workflow import DESIGN, navigate
from covscreen.mcp.server import dispatch
from test_receptor_evidence_workflow import (
    design, graph_design, pose, inputs, bound, planned_receptor, isolated, completed, args, jobs)


@pytest.fixture
def reviewed(completed):
    case, launches, folder, manager, run = completed
    dispatch(case[0], DESIGN, args(case, 'evaluate_receptor'))
    return completed


def configure(case):
    runtime = case[0] / 'covdock-runtime'
    runtime.mkdir()
    for name in ('run', 'covalent_docking', 'vendor.lic'):
        (runtime / name).write_text('INERT SOFTWARE FIXTURE; NEVER EXECUTE\n')
    path = case[0] / 'configs/local.inspire.yaml'
    data = json.loads(path.read_text())
    data['schrodinger'] = {'enabled': True, 'root': str(runtime), 'license_file': str(runtime / 'vendor.lic')}
    path.write_text(json.dumps(data))


def test_prepared_receptor_to_bound_covdock_plan_without_job_or_scientific_acceptance(reviewed):
    case, launches, folder, manager, run = reviewed
    before = jobs(case)
    result = dispatch(case[0], DESIGN, args(case, 'prepare_covdock_input'))
    reference = result['refinement_input']['covdock_input']
    requirements = result['result']['review_requirements']
    assert requirements['status'] == 'REVIEW_REQUIRED'
    assert requirements['construct_audit']['unaligned_entity_positions'] == [1, 2, 3, 4, 5]
    assert requirements['excluded_environment']['nonwater_components'][0]['resname'] == 'AQ4'
    assert not requirements['biological_suitability_verified']
    assert reference['attachment_residue'] == 'A:773'  # Canonical C797 is not the receptor author number.
    assert reference['planning_only'] and not reference['execution_authorized']
    assert dispatch(case[0], DESIGN, args(case, 'prepare_covdock_input'))['refinement_input']['covdock_input'] == reference
    waiting = dispatch(case[0], DESIGN, args(case, 'check_covdock_runtime'))
    assert waiting['refinement_input']['covdock_plan']['runtime_check'] == 'WAITING_RUNTIME_CONFIGURATION'
    configure(case)
    result = dispatch(case[0], DESIGN, args(case, 'check_covdock_runtime'))
    plan = result['refinement_input']['covdock_plan']
    assert plan['runtime_check'] == 'PLAN_COMPILED_ENVIRONMENT_REVIEW_REQUIRED'
    assert not plan['environment_review_completed'] and not plan['license_availability_verified']
    spec = plan['spec']
    assert spec['parameters']['input_score_source'] == 'core'
    assert spec['parameters']['refinement_input_contract']['input_count'] == reference['candidate_count']
    assert '--prepare-receptor' in spec['command'] and '--resume' not in spec['command']
    assert '--runtime-lib-dir' not in spec['command']  # Missing optional path must not become cwd.
    assert spec['resource_budget']['gpu_count'] == 0
    state = json.loads(case[2].read_text())
    payload = load(case[0], state)
    assert set(payload['lineage_sha256']).issubset(spec['input_sha256'])
    assert spec['input_sha256'][reference['path']] == reference['manifest_sha256']
    assert spec['provenance']['config_binding'] == 'immutable-runtime-config-v1'
    assert spec['parameters']['attachment_residue'] == 'A:773'
    # A plan cannot be turned into a current-study execution by flipping mode.
    request = request_for(case[0], state).model_copy(update={'execution_mode': 'execute',
        'human_approved': True, 'approval_note': 'Software fixture must not launch'})
    with pytest.raises(ValueError, match='positive environment assessment'):
        JobManager(case[0])._prepare_submission(request)
    restored = navigate(case[0], 'resume', workflow_id=case[1])
    assert restored['refinement_input']['covdock_plan'] == plan
    assert 'start_covdock' not in restored['workflow']['tools'][DESIGN]['actions']
    assert jobs(case) == before and len(launches) == 1


def test_changed_bound_inputs_withdraw_plan_and_preserve_preparation_job(reviewed):
    case, launches, folder, manager, run = reviewed
    dispatch(case[0], DESIGN, args(case, 'prepare_covdock_input'))
    configure(case)
    result = dispatch(case[0], DESIGN, args(case, 'check_covdock_runtime'))
    reference = result['refinement_input']['covdock_input']
    path = case[0] / reference['path']
    value = json.loads(path.read_text())
    value['attachment_residue'] = 'A:797'
    path.write_text(json.dumps(value))
    restored = navigate(case[0], 'status', workflow_id=case[1])
    handoff = restored['refinement_input']
    assert 'covdock_input' not in handoff and 'covdock_plan' not in handoff
    assert handoff['covdock_input_error']['artifact_id'] == reference['artifact_id']
    assert handoff['receptor_preparation']['run_id'] == run
    assert len(launches) == 1 and len(jobs(case)) == 2


def test_incorrect_stage_cannot_supply_free_receptor_or_candidate_paths(completed):
    case, launches, folder, manager, run = completed
    with pytest.raises(ValueError, match='verified receptor review'):
        dispatch(case[0], DESIGN, args(case, 'prepare_covdock_input'))
    with pytest.raises(ValueError):
        dispatch(case[0], DESIGN, {**args(case, 'prepare_covdock_input'), 'receptor': 'foreign.pdb'})
    assert len(launches) == 1


@pytest.mark.parametrize('target', [
    {'target': 'P04626', 'site': 'C797', 'taxon': 9606},
    {'target': 'P00533', 'site': 'C798', 'taxon': 9606},
    {'target': 'P00533', 'site': 'C797', 'taxon': 10090},
])
def test_covdock_rejects_unsupported_target_site_or_species(target, tmp_path):
    from covscreen.ligand_covdock_input import build
    with pytest.raises(ValueError, match='explicit EGFR C797 / human identity'):
        build(tmp_path, {'target': target})


def test_accession_alias_cannot_replace_an_existing_sealed_request(reviewed):
    from covscreen.ligand_covdock_input import build
    case, launches, folder, manager, run = reviewed
    state = json.loads(case[2].read_text())
    state['target']['target'] = 'P00533'
    # Both names are supported at intake, but an existing study and its frozen
    # target must remain bound to the original request and evidence bytes.
    with pytest.raises(ValueError):
        build(case[0], state)
    assert len(launches) == 1
