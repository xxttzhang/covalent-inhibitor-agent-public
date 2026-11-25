"""Signed artificial ledger and byte-level receipt tests; no scientific jobs."""
import hashlib
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from covscreen.approval_scope import normalize_spec
from covscreen.branch_guard import quarantine_job
from covscreen.execution import JobManager
from covscreen.jobs import ArtifactRecord, ArtifactRegistry, JobSpec, JobSubmissionRequest, RunResult, utc_now
from covscreen.native_approval import CURRENT, seal_spec, signed
from covscreen.submission_identity import submit_once
from covscreen_de_novo import independent_review as adapter
from covscreen_de_novo.prereaction_receipt import retained


@pytest.fixture
def evidence(tmp_path, monkeypatch, request):
    verdict = getattr(request, "param", "FAIL")
    key = tmp_path/'approval.key'; key.write_bytes(b'isolated-budget-receipt-fixture-key'); key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(key))
    source = 'denovo-review-'+'a'*32
    current = dict(study_id='study-artificial', project_id='project-artificial', source_id=source,
        source_manifest_sha256='a'*64, candidate_id='sample-artificial:0:raw')
    registry = ArtifactRegistry(tmp_path)
    spec = JobSpec(run_id='fixture-vina-run', request_id=source+'-prereaction',
        project_id=current['project_id'], route='de-novo', engine='de-novo-prereaction-vina',
        execution_mode='execute', human_approved=True, approval_note='Artificial software fixture only',
        working_directory=str(tmp_path), output_directory=str(tmp_path/'output'), command=['never-execute'],
        resource_budget={'max_runtime_minutes':30}, parameters={'workflow_id':current['study_id'],
            'denovo_source_id':source, 'denovo_source_manifest_sha256':'a'*64, 'prereaction_runtime_sha256':'b'*64})
    folder = adapter.folder_for(tmp_path, source); folder.mkdir(parents=True)
    normalized = normalize_spec(spec)
    normalized.update(execution_mode='plan', human_approved=False, approval_note=None)
    plan = dict(schema='denovo-prereaction-plan-v1', source_id=source, source_manifest_sha256='a'*64,
        spec=normalized, spec_sha256=hashlib.sha256(adapter.encoded(normalized)).hexdigest())
    (folder/'prereaction-plan.json').write_bytes(adapter.encoded(plan))
    token = CURRENT.set(signed({'scope':{'kind':'job','spec':normalize_spec(spec)}}))
    try: spec.provenance['native_approval'] = seal_spec(spec)
    finally: CURRENT.reset(token)
    job = RunResult(run_id=spec.run_id, project_id=spec.project_id, route=spec.route, engine=spec.engine,
        state='COMPLETED', return_code=0, submitted_at=utc_now(), message='fixture only', stdout_path='stdout', stderr_path='stderr')
    registry.create(spec, job)
    output = registry.job_dir(spec.run_id)/'artifacts/prereaction'; output.mkdir(parents=True)
    report = {key:current[key] for key in ('study_id','source_manifest_sha256','candidate_id')}
    report.update(independent_prereaction_evaluation=verdict, control_prereaction_evaluation=verdict,
                  fixture='Artificial metadata, not a scientific calculation')
    (output/'report.json').write_bytes(adapter.encoded(report))
    (output/'poses.pdbqt').write_text('artificial pose bytes')
    files = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir()}
    (output/'manifest.json').write_bytes(adapter.encoded(dict(schema='denovo-prereaction-artifacts-v1',
        source_manifest_sha256='a'*64, runtime_sha256='b'*64, files=files)))
    for i,path in enumerate(output.iterdir()):
        job.artifacts.append(ArtifactRecord(artifact_id='fixture-'+str(i), run_id=spec.run_id, role='report',
            path=str(path.relative_to(tmp_path)), media_type='application/octet-stream',
            size_bytes=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    registry.write_result(job)
    request = JobSubmissionRequest(request_id=spec.request_id, project_id=spec.project_id, route=spec.route,
        engine=spec.engine, resource_budget=spec.resource_budget, execution_mode='execute', parameters=spec.parameters)
    submit_once(JobManager(tmp_path), request, lambda request: registry.load_result(spec.run_id), planned_run_id=spec.run_id)
    prior = dict(current, schema='denovo-prereaction-observation-v1', run_id=spec.run_id,
        state='COMPLETED', return_code=0, scientific_result_verified=True, report=report,
        manifest_sha256=hashlib.sha256((output/'manifest.json').read_bytes()).hexdigest(),
        independent_prereaction_evaluation=verdict, control_prereaction_evaluation=verdict)
    history = folder/'prereaction-observations'; history.mkdir()
    raw = adapter.encoded(prior); path = history/(hashlib.sha256(raw).hexdigest()+'.json');path.write_bytes(raw)
    def forbidden(*args, **kwargs): raise AssertionError('Status must not launch a subprocess')
    monkeypatch.setattr('subprocess.Popen', forbidden)
    return SimpleNamespace(root=tmp_path,current=current,registry=registry,spec=spec,job=job,
        folder=folder,output=output,prior=prior,path=path)


def test_status_preserves_vina_failure_without_adduct_review_and_without_resubmission(evidence, monkeypatch):
    e=evidence
    def observe(*args):
        return ({**e.current, 'run_id':'fixture-covdock-run','state':'TIMED_OUT',
            'independent_adduct_evaluation':'NOT_PERFORMED', **adapter.requirements()},
            (None,None,SimpleNamespace(state='TIMED_OUT',return_code=-15)))
    monkeypatch.setattr(adapter,'observe',observe)
    before={str(p):p.read_bytes() for p in (e.root/'state').rglob('*.json')}
    for _ in range(2):
        result=adapter.step(e.root,'status',e.current['source_id'],'a'*64)
        assert result['state']=='TIMED_OUT'
        assert result['independent_adduct_evaluation']=='ENGINE_FAILED'
        assert result['independent_prereaction_evaluation']==result['control_prereaction_evaluation']=='FAIL'
        assert result['prereaction_review']['evidence_revalidated_by_status'] is True
        assert result['prereaction_receipt_recomputed_by_status'] is False
        assert not result['ready_for_scientific_handoff']
        assert result['native_md_admission']=='NOT_PERFORMED'
    for name,raw in before.items(): assert Path(name).read_bytes()==raw
    assert len(list(e.registry.root.glob('*/spec.json')))==1


@pytest.mark.parametrize('change', ['receipt_bytes','foreign_candidate','changed_pose','conflicting_report','quarantine'])
def test_retained_review_rejects_changed_or_misattributed_evidence(evidence,change):
    e=evidence
    if change=='receipt_bytes': e.path.write_bytes(e.path.read_bytes()+b' ')
    elif change in ('foreign_candidate','conflicting_report'):
        changed=json.loads(e.path.read_text())
        if change=='foreign_candidate': changed['candidate_id']='another-candidate'
        else: changed['report']['independent_prereaction_evaluation']='PASS'
        raw=adapter.encoded(changed)
        (e.path.parent/(hashlib.sha256(raw).hexdigest()+'.json')).write_bytes(raw)
    elif change=='changed_pose': (e.output/'poses.pdbqt').write_text('altered pose')
    else: quarantine_job(e.root,e.spec.run_id,'test','artificial failure')
    with pytest.raises(ValueError): retained(e.root,e.current)


def test_completion_without_prior_independent_review_does_not_invent_verification(evidence):
    e=evidence; e.path.unlink()
    result=retained(e.root,e.current)
    assert result['state']=='COMPLETED'
    assert result['independent_prereaction_evaluation']=='NOT_PERFORMED'
    assert result['scientific_result_verified'] is False
    assert result['native_md_admission_granted'] is False


def test_aggregate_marks_broken_prior_evidence_insufficient_without_losing_engine_state(evidence,monkeypatch):
    e=evidence; (e.output/'poses.pdbqt').write_text('changed')
    monkeypatch.setattr(adapter,'observe',lambda *args: ({**e.current,'state':'TIMED_OUT',**adapter.requirements()},
        (None,None,SimpleNamespace(state='TIMED_OUT',return_code=-15))))
    result=adapter.step(e.root,'status',e.current['source_id'],'a'*64)
    assert result['state']=='TIMED_OUT'
    assert result['independent_adduct_evaluation']=='ENGINE_FAILED'
    assert result['independent_prereaction_evaluation']=='EVIDENCE_INSUFFICIENT'
    assert not result['ready_for_scientific_handoff']


@pytest.mark.parametrize('evidence', ['PASS'], indirect=True)
def test_retained_pass_is_not_fresh_measurement_or_md_admission(evidence):
    result=retained(evidence.root,evidence.current)
    assert result['independent_prereaction_evaluation']=='PASS'
    assert result['scientific_result_verified'] is True
    assert result['receipt_recomputed_by_status'] is False
    assert result['native_md_admission_granted'] is False
