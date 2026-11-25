"""Signed VS-to-LO handoff, without vendor or scheduler computation.

Only positive VS geometry measurement is controlled. Original source files,
registered products, independent approval and registration recovery are real.
Synthetic coordinates are not scientific acceptance evidence.
"""
import json

import pytest

from covscreen.execution import JobManager
from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.library_lo_input import load as load_input
from covscreen.mcp.server import dispatch
from covscreen.workflow_store import load
from test_library_workflow import call, state_file
from test_library_vs_evidence import completed
from test_library_vs_input import assessment
from test_library_vs_prescreen import grant
from test_receptor_handoff import bound


@pytest.fixture
def eligible(completed, monkeypatch):
    from covscreen import library_vs_evidence
    from covscreen.library_vs_geometry import assess
    def measure(row, reference, receptor_path, attachment, policy):
        metrics = {'covalent_cs_distance_angstrom': 1.8,
            'product_s_cbeta_calpha_angle_degrees': 110., 'clash_fraction': 0.,
            'severe_clash_pair_count': 0, 'ligand_local_relaxation_energy_kcal_mol': 0.,
            'reference_contact_site_retention': 1.}
        return {'pose_index': row['pose_index'], 'candidate_id': row['candidate_id'],
            'input_index': row['input_index'], 'covdock_vs_score': row['score'],
            'metrics': metrics, 'assessment': assess(metrics, policy), 'md_ready': False}
    monkeypatch.setattr(library_vs_evidence, 'measure', measure)
    root, observed, raw = completed
    checked = call(root, observed, RESEARCH, 'evaluate_vs')
    return root, checked, raw


def test_subset_private_plan_independent_approval_and_lost_reply(eligible, monkeypatch):
    root, checked, raw = eligible
    candidates = checked['vs_execution']['evaluation_summary']['candidates']
    candidate, missing = candidates[0]['candidate_id'], candidates[1]['candidate_id']
    for names in ([missing], [candidate, candidate], ['foreign-candidate'], []):
        with pytest.raises(ValueError):
            call(root, checked, RESEARCH, 'prepare_vs_refinement', candidate_ids=names,
                 reason='Explicit selection must reject invalid or unmeasured candidates')
    prepared = call(root, checked, RESEARCH, 'prepare_vs_refinement', candidate_ids=[candidate],
        reason='Select the measured VS candidate for separate LO refinement')
    from test_library_vs_input import inspect_native_evidence
    prepared = inspect_native_evidence(root, prepared, 'read_lo_input')
    state = load(state_file(root, prepared))
    payload = load_input(root, state)
    assert payload['source_run_id'] == checked['vs_execution']['run_id']
    assert payload['candidate_count'] == 1 and payload['attachment_residue'] == 'A:773'
    assert not payload['md_ready'] and not state.get('refinement_handoff')
    table = (root/state['lo_input']['directory']/'candidates.csv').read_text()
    assert 'schrodinger-covdock-vs' in table and 'carsidock_score' not in table
    unreviewed = call(root, prepared, RESEARCH, 'check_lo_runtime')
    assert unreviewed['lo_runtime']['state'] == 'PLAN_COMPILED_ENVIRONMENT_REVIEW_REQUIRED'
    assert 'start_lo' not in unreviewed['workflow']['tools'][RESEARCH]['actions']
    with pytest.raises(ValueError, match='Invalid environment judgment'):
        call(root, unreviewed, RESEARCH, 'review_lo_environment',
             environment_review=assessment(prepared, 'suitable_for_exploratory_vs'))
    reviewed = call(root, unreviewed, RESEARCH, 'review_lo_environment',
        environment_review=assessment(prepared, 'suitable_for_exploratory_refinement'))
    planned = call(root, reviewed, RESEARCH, 'check_lo_runtime')
    assert planned['workflow']['stage'] == 'LO_PLAN_READY'
    state = load(state_file(root, planned))
    private = root/state['lo_plan']['path']
    assert private.stat().st_mode & 0o777 == 0o600
    frozen = json.loads(private.read_text())
    assert frozen['engine'] == 'schrodinger-covdock-lo'
    assert frozen['provenance']['worker_backend'] == 'external-hpc'
    assert frozen['parameters']['input_row_count'] == 1
    assert frozen['resource_budget']['max_candidates'] == 1
    assert str(root/'vendor') not in json.dumps(planned)
    private_bytes = private.read_bytes()
    private.write_bytes(private_bytes + b' changed')
    with pytest.raises(ValueError, match='private plan changed'):
        grant(root, planned, action='start_lo')
    private.write_bytes(private_bytes)
    with pytest.raises(ValueError, match='approval'):
        call(root, planned, RESEARCH, 'start_lo')
    original_submit = JobManager.submit
    registered = []
    def lose_response(manager, request):
        value = original_submit(manager, request)
        registered.append(value.run_id)
        raise OSError('Controlled loss after LO registration')
    monkeypatch.setattr(JobManager, 'submit', lose_response)
    with pytest.raises(OSError, match='Controlled loss'):
        dispatch(root, RESEARCH, grant(root, planned, action='start_lo'))
    pending = load(state_file(root, planned))
    assert pending['pending']['action'] == 'start_lo'
    recovered = dispatch(root, ENTRY, {'action': 'reconcile', 'workflow_id': state['workflow_id'],
        'expected_revision': pending['revision']})
    assert recovered['pending'] is None
    assert recovered['lo_execution']['run_id'] == registered[0]
    assert recovered['lo_execution']['job_identity_verified']
    assert recovered['execution']['task_ids'] == registered
    assert 'start_lo' not in recovered['workflow']['tools'][RESEARCH]['actions']
    assert 'reconsider' not in recovered['workflow']['tools'][ENTRY]['actions']
    observed = call(root, recovered, RESEARCH, 'lo_status')
    assert observed['lo_execution']['job_state'] == 'QUEUED'
    before = raw.read_bytes()
    raw.write_bytes(before + b' changed')
    rejected = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': state['workflow_id']})
    assert rejected['execution']['state'] == 'outcome_unknown'
    assert rejected['lo_execution']['evidence_error']
    assert not rejected['lo_execution']['job_identity_verified']
    raw.write_bytes(before)
    restored = dispatch(root, ENTRY, {'action': 'resume', 'workflow_id': state['workflow_id']})
    assert restored['lo_execution']['run_id'] == registered[0]
    assert restored['lo_execution']['job_identity_verified']
    assert not restored['lo_execution'].get('evidence_error')
    stable = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': state['workflow_id']})
    assert stable['workflow']['revision'] == restored['workflow']['revision']
    assert len(registered) == 1 and len(list((root/'state/jobs').glob('*/spec.json'))) == 4
    assert not (JobManager(root).registry.job_dir(registered[0])/'external_hpc.json').exists()
    # Artificial registered LO outputs exercise real manifest/gate replay.
    # Only the proprietary SDK reader is replaced; no scientific claim follows.
    from covscreen import refinement_output
    from covdock_readback_fixture import outputs
    manager = JobManager(root)
    spec = manager.registry.load_spec(registered[0])
    _, sdk = outputs(root, manager, spec, partial=False)
    monkeypatch.setattr(refinement_output, '_covdock_sdk', sdk)
    terminal = call(root, stable, RESEARCH, 'lo_status')
    evaluated = call(root, terminal, RESEARCH, 'evaluate_lo')
    summary = evaluated['lo_execution']['evaluation_summary']
    assert summary['candidate_count'] == 1
    assert summary['candidates'][0]['gate']['physical_validity'] == 'PASS'
    assert summary['candidates'][0]['gate']['cross_method_consensus'] == 'NA'
    assert summary['candidates'][0]['gate']['review_eligible']
    assert not summary['policy']['cross_method_consensus_required']
    assert not evaluated['lo_execution']['md_ready']
    reported = call(root, evaluated, RESEARCH, 'report_lo')
    report = reported['lo_execution']['report']
    assert 'MD is outside this beta' in (root/report['path']).read_text()
    changed = root/report['path']
    contents = changed.read_bytes()
    changed.write_bytes(contents + b' forged')
    rejected = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': state['workflow_id']})
    assert 'report' not in rejected['lo_execution']
    assert 'evaluation_summary' not in rejected['lo_execution']
    changed.write_bytes(contents)
    restored = dispatch(root, ENTRY, {'action': 'resume', 'workflow_id': state['workflow_id']})
    assert restored['lo_execution']['evaluation_summary'] == summary
    final = call(root, restored, RESEARCH, 'report_lo')
    assert final['lo_execution']['report'] == report
    assert len(registered) == 1
    reconsidered = call(root, final, ENTRY, 'reconsider', return_to='structure',
        reason='Archive the completed software-only LO branch and preserve its provenance')
    archived = load(state_file(root, reconsidered))
    assert archived['lo_execution'] is None and archived['lo_input'] is None
    assert archived['branches'][-1]['lo_execution']['run_id'] == registered[0]
    assert archived['branches'][-1]['lo_execution']['report'] == report
    assert len(list((root/'state/jobs').glob('*/spec.json'))) == 4
