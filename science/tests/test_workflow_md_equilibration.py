"""Workflow integration using explicit software output fixtures, never real MD."""
import json
from pathlib import Path

import pytest
from covscreen.execution import JobManager
from covscreen.jobs import sha256_file
from covscreen.library_workflow import RESEARCH
from covscreen.mcp.server import dispatch
from covscreen.workflow_store import load
from test_workflow_md_execution import research, call, approval
from test_workflow_md_short_qc import ready as prepared, start as start_qc, completed as qc_output, register_output


def ready(fixture, monkeypatch):
    prepared(fixture, monkeypatch)
    run = start_qc(fixture)
    qc_output(fixture, run)
    assert call(fixture, 'md_short_qc_status')['md_execution']['verification'] == 'verified'
    workspace = Path(__file__).resolve().parents[2]
    with (fixture[0] / 'configs/local.inspire.yaml').open('a') as stream:
        stream.write('\nopenmm_equilibration:\n  enabled: true\n'
            f"  python: {workspace / '.tooling/md-interoperability-20261002/bin/python'}\n"
            f"  pythonpath: {workspace / '.tooling/md-interoperability-20261002'}\n"
            "  platform: CPU\n  platform_properties:\n    Threads: '1'\n")


def start(fixture):
    call(fixture, 'plan_md_equilibration', integrator_seed=11, barostat_seed=12)
    return dispatch(fixture[0], RESEARCH, approval(fixture, 'start_md_equilibration'))['md_execution']['run_id']


def completed(fixture, run, admitted=True, *, parent=None):
    manager = JobManager(fixture[0])
    spec = manager.registry.load_spec(run)
    parent = parent or load(fixture[1])['md_execution']['plan']['input']
    short = json.loads((manager.registry.job_dir(parent['run_id']) / 'artifacts/openmm-short-qc/short-qc.json').read_text())
    folder = manager.registry.job_dir(run) / 'artifacts/openmm-equilibration'
    folder.mkdir(parents=True)
    rows = [dict(step=i*5000, time_ps=i*10, potential_energy_kj_mol=-2, kinetic_energy_kj_mol=1,
        temperature_k=300 if admitted else 350, volume_nm3=10, density_kg_m3=1000,
        covalent_distance_angstrom=1.8, max_relative_constraint_error=1e-6) for i in range(1,51)]
    (folder/'frames.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    for name in ('final-positions-nm.npy','final-velocities-nm-ps.npy','final-box-vectors-nm.npy','final-state.xml','checkpoint.chk'):
        (folder/name).write_bytes(b'SOFTWARE FIXTURE ONLY; NOT A SCIENTIFIC RESULT')
    value = dict(schema='openmm-equilibration-artifact-v1', system_id=parent['candidate_id'], atoms=short['atoms'],
        parents={**{key:short['parents'][key] for key in ('parameter_audit_sha256','minimization_manifest_sha256')},
            'short_qc_manifest_sha256':parent['manifest_sha256']},
        protocol=dict(ensemble='NPT',unrestrained=True,platform=spec.parameters['platform'],
            platform_properties=spec.parameters['platform_properties'],duration_ps=500.0,steps=250000,
            report_interval_ps=10.0,timestep_fs=2.0,temperature_k=300.0,pressure_bar=1.01325,
            friction_per_ps=1.0,barostat_interval=25,integrator_seed=spec.parameters["integrator_seed"],barostat_seed=spec.parameters["barostat_seed"]),
        equilibration_protocol_completed=True,admission_duration_met=True,equilibrium_convergence_claimed=False,
        production_started=False,md_system_ready=False,campaign_ready=False,
        artifacts={p.name:sha256_file(p) for p in folder.iterdir()},frames=50,final_window_frames=10,last_frame=rows[-1],
        checks=dict(temperature_window=admitted,density_window=True,volume_stability=True,constraint_stability=True,covalent_geometry=True),
        production_review_ready=admitted,status='NPT_EQUILIBRATION_COMPLETED_PRODUCTION_REVIEW_PENDING' if admitted else 'EQUILIBRATION_DIAGNOSTIC_COMPLETE_NOT_ADMITTED')
    (folder/'equilibration.json').write_text(json.dumps(value))
    register_output(fixture, run, folder)
    return folder


@pytest.mark.parametrize('admitted', [True, False])
def test_completion_is_distinct_from_production_admission(research, monkeypatch, admitted):
    ready(research, monkeypatch)
    run = start(research)
    completed(research, run, admitted)
    result = call(research, 'md_equilibration_status')['md_execution']
    assert result['verification'] == 'verified'
    assert result['equilibration_protocol_completed'] is True
    assert result['production_review_ready'] is admitted
    assert result['md_system_ready'] is False
    assert result['equilibration']['assessment'] == ('review_required' if admitted else 'rejected')


def test_equilibration_parent_mutation_revokes_receipt(research, monkeypatch):
    ready(research, monkeypatch)
    run = start(research)
    completed(research, run)
    assert call(research, 'md_equilibration_status')['md_execution']['verification'] == 'verified'
    parent = load(research[1])['md_execution']['parent']['run_id']
    path = JobManager(research[0]).registry.job_dir(parent) / 'artifacts/openmm-short-qc/final-positions-nm.npy'
    path.write_bytes(b'CHANGED')
    with pytest.raises(ValueError):
        call(research, 'md_equilibration_status')
    assert load(research[1])['md_execution']['verification'] == 'evidence_rejected'


@pytest.mark.parametrize('changed', ['checks', 'protocol', 'duration', 'schedule', 'missing_output'])
def test_registered_inconsistent_equilibration_cannot_pass(research, monkeypatch, changed):
    ready(research, monkeypatch)
    run = start(research)
    folder = completed(research, run, admitted=False if changed == 'checks' else True)
    manifest = folder/'equilibration.json'
    value = json.loads(manifest.read_text())
    if changed == 'checks':
        value['checks']['temperature_window'] = True
        value['production_review_ready'] = True
        value['status'] = 'NPT_EQUILIBRATION_COMPLETED_PRODUCTION_REVIEW_PENDING'
    elif changed == 'protocol':
        value['protocol']['integrator_seed'] = 999
    elif changed == 'duration':
        value['admission_duration_met'] = False
    elif changed == 'missing_output':
        (folder/'checkpoint.chk').unlink()
    else:
        rows = [json.loads(line) for line in (folder/'frames.jsonl').read_text().splitlines()]
        rows[0]['time_ps'] = 99
        (folder/'frames.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
        value['artifacts']['frames.jsonl'] = sha256_file(folder/'frames.jsonl')
    manifest.write_text(json.dumps(value))
    register_output(research, run, folder)
    result = call(research, 'md_equilibration_status')['md_execution']
    assert result['verification'] == 'evidence_rejected'
    assert result['equilibration'] is None
    assert result['production_review_ready'] is False


def test_equilibration_plan_requires_approval_and_releases_to_verified_qc(research, monkeypatch):
    ready(research, monkeypatch)
    parent = load(research[1])['md_execution']['run_id']
    planned = call(research, 'plan_md_equilibration', integrator_seed=11, barostat_seed=12,
        resource_budget=dict(max_runtime_minutes=720, cpu_count=4, memory_gb=32, gpu_count=0, max_candidates=1))
    fields = planned['workflow']['tools'][RESEARCH]['fields']
    assert 'integrator_seed' in fields and 'barostat_seed' in fields
    assert 'steps' not in fields and 'short_qc_run_id' not in fields
    with pytest.raises(ValueError, match='receipt required'):
        call(research, 'start_md_equilibration', human_approved=True)
    released = call(research, 'release_md_stage', reason='Revise the equilibration plan before allocating compute resources.')
    assert released['workflow']['stage'] == 'MD_SHORT_QC_REVIEW'
    assert load(research[1])['md_execution']['run_id'] == parent
    assert load(research[1])['md_execution']['verification'] == 'verified'


def test_equilibration_rejects_gpu_budget_for_registered_cpu_runtime(research, monkeypatch):
    ready(research, monkeypatch)
    revision = load(research[1])['revision']
    with pytest.raises(ValueError, match='does not match'):
        call(research, 'plan_md_equilibration', integrator_seed=11, barostat_seed=12,
            resource_budget=dict(max_runtime_minutes=720, cpu_count=4, memory_gb=32, gpu_count=1, max_candidates=1))
    assert load(research[1])['revision'] == revision


def test_lost_start_reply_recovers_and_cancels_original_equilibration(research, monkeypatch):
    from covscreen.execution import execute_persisted_job
    from test_workflow_md_execution import register
    ready(research, monkeypatch)
    call(research, 'plan_md_equilibration', integrator_seed=11, barostat_seed=12)
    def interrupted(manager, request):
        register(manager, request)
        raise ConnectionError('Lost equilibration reply')
    monkeypatch.setattr(JobManager, 'submit', interrupted)
    with pytest.raises(ConnectionError):
        dispatch(research[0], RESEARCH, approval(research, 'start_md_equilibration'))
    restored = call(research, 'reconcile')
    assert restored['pending'] is None
    run = restored['md_execution']['run_id']
    result = dispatch(research[0], RESEARCH, approval(research, 'cancel_md_stage'))
    assert result['md_execution']['cancellation_requested'] is True
    assert result['md_execution']['cancellation_confirmed'] is False
    execute_persisted_job(research[0], run)
    result = call(research, 'md_equilibration_status')
    assert result['md_execution']['run_id'] == run
    assert result['md_execution']['cancellation_confirmed'] is True
