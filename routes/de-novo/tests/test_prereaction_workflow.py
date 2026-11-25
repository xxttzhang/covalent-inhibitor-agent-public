"""Protocol tests on explicit artificial source metadata; no scientific claims."""
from copy import deepcopy
import hashlib
import json

import pytest

from covscreen.execution import JobManager, _engine_environment
from covscreen.jobs import JobSpec
from covscreen.approval_scope import normalize_spec
from covscreen_de_novo import prereaction_workflow as workflow

SOURCE = 'denovo-review-'+'a'*32
DIGEST = 'a'*64


def bind_source(monkeypatch, root):
    # Source integrity itself is exercised by the existing real source tests.
    # This fixture isolates the execution/approval boundary from expensive generation.
    original = {'project_id':'artificial-project', 'study_id':'artificial-study', 'candidate_id':'artificial-candidate'}
    monkeypatch.setattr(workflow, 'source', lambda *args:(root,original))


def test_plan_is_bounded_and_never_claims_approval(tmp_path, monkeypatch):
    bind_source(monkeypatch,tmp_path)
    request = workflow.request_for(tmp_path,SOURCE,DIGEST)
    assert request.route == 'de-novo'
    assert request.engine == workflow.ENGINE
    assert request.request_id == SOURCE+'-prereaction'
    assert request.resource_budget.model_dump() == dict(gpu_count=0,cpu_count=1,memory_gb=8,
        max_runtime_minutes=30,max_candidates=2)
    assert request.execution_mode == 'plan' and not request.human_approved
    workflow.validate_request(tmp_path,request)


@pytest.mark.parametrize('change', [
    {'route':'ligand-first'}, {'engine':'schrodinger-covdock-lo'}, {'request_id':'changed-request'},
    {'project_id':'another-project'}, {'attempt':2}, {'parent_run_id':'another-run'},
    {'parameters':{'workflow_id':'other-study','denovo_source_id':SOURCE,'denovo_source_manifest_sha256':DIGEST}},
])
def test_raw_request_cannot_change_scope(tmp_path,monkeypatch,change):
    bind_source(monkeypatch,tmp_path)
    request=workflow.request_for(tmp_path,SOURCE,DIGEST).model_copy(update=change)
    with pytest.raises(ValueError,match='bounded native study'):
        workflow.validate_request(tmp_path,request)


@pytest.mark.parametrize('field,value', [('gpu_count',1),('cpu_count',2),('max_candidates',3),('max_runtime_minutes',31)])
def test_raw_submit_cannot_expand_budget(tmp_path,monkeypatch,field,value):
    bind_source(monkeypatch,tmp_path)
    request=workflow.request_for(tmp_path,SOURCE,DIGEST)
    request.resource_budget=request.resource_budget.model_copy(update={field:value})
    with pytest.raises(ValueError,match='bounded native study'):
        workflow.validate_request(tmp_path,request)


def test_execute_requires_a_previously_frozen_plan(tmp_path,monkeypatch):
    bind_source(monkeypatch,tmp_path)
    request=workflow.request_for(tmp_path,SOURCE,DIGEST,execution_mode='execute')
    with pytest.raises(ValueError,match='Freeze the independent'):
        workflow.validate_request(tmp_path,request)
    assert not (tmp_path/'state/jobs').exists()


def spec(root):
    return JobSpec(run_id='artificial-prereaction-run', request_id=SOURCE+'-prereaction',
        project_id='artificial-project',route='de-novo',engine=workflow.ENGINE,
        command=['registered-python','registered-worker.py'],working_directory=str(root),
        output_directory=str(root/'output'),parameters={'denovo_source_id':SOURCE,
            'denovo_source_manifest_sha256':DIGEST,'prereaction_runtime_sha256':'b'*64},
        input_sha256={'frozen-input':'c'*64},provenance={'worker_backend':'local'},
        resource_budget={'cpu_count':1,'gpu_count':0,'max_runtime_minutes':30,'max_candidates':2,'memory_gb':8})


def test_compiled_plan_rejects_changed_runtime_and_keeps_old_bytes(tmp_path):
    planned=spec(tmp_path)
    folder=workflow.folder_for(tmp_path,SOURCE);folder.mkdir(parents=True)
    normalized=normalize_spec(planned)
    value={'schema':'denovo-prereaction-plan-v1','source_id':SOURCE,'source_manifest_sha256':DIGEST,
        'spec':normalized,'spec_sha256':hashlib.sha256(workflow.encoded(normalized)).hexdigest()}
    path=folder/'prereaction-plan.json';raw=workflow.encoded(value);path.write_bytes(raw)
    executed=planned.model_copy(update={'execution_mode':'execute','human_approved':True,'approval_note':'artificial unit test only'})
    workflow.validate_compiled_spec(tmp_path,executed)
    altered=executed.model_copy(deep=True)
    altered.parameters['prereaction_runtime_sha256']='d'*64
    with pytest.raises(ValueError,match='changed after freeze'):
        workflow.validate_compiled_spec(tmp_path,altered)
    assert path.read_bytes()==raw


def test_missing_runtime_cannot_fall_back_to_model_python(tmp_path,monkeypatch):
    bind_source(monkeypatch,tmp_path)
    manager=JobManager(tmp_path)
    with pytest.raises(ValueError,match='Register the independent'):
        workflow.build(manager,'artificial-run',{'denovo_source_id':SOURCE,'denovo_source_manifest_sha256':DIGEST})


def test_independent_cpu_job_does_not_use_covdock_hpc_default(tmp_path,monkeypatch):
    bind_source(monkeypatch,tmp_path)
    manager=JobManager(tmp_path)
    manager.config={'runtime':{'worker_backend':'external-hpc'}}
    assert manager.worker_backend(workflow.request_for(tmp_path,SOURCE,DIGEST))=='local'


def test_worker_drops_python_and_loader_injection(tmp_path,monkeypatch):
    monkeypatch.setenv('PYTHONPATH','/artificial/untrusted/source')
    monkeypatch.setenv('LD_LIBRARY_PATH','/artificial/untrusted/library')
    monkeypatch.setenv('PYTHONHOME','/artificial/untrusted/python')
    observed=_engine_environment(spec(tmp_path),tmp_path)
    assert not {'PYTHONPATH','LD_LIBRARY_PATH','PYTHONHOME'} & set(observed)
    assert observed['OMP_NUM_THREADS']=='1'


def test_no_receipt_remains_not_submitted_and_cannot_grant_md(tmp_path,monkeypatch):
    bind_source(monkeypatch,tmp_path)
    result=workflow.observe(tmp_path,SOURCE,DIGEST,evaluate=True)
    assert result['state']=='NOT_SUBMITTED'
    assert result['independent_prereaction_evaluation']=='NOT_PERFORMED'
    assert result['scientific_result_verified'] is False
    assert result['native_md_admission_granted'] is False
