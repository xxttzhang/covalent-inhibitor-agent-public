import importlib.util
import json
from pathlib import Path
import sys
import pytest
from covscreen import external_openmm as ext
from covscreen.jobs import JobSpec, ResourceBudget, ArtifactRegistry, RunResult, utc_now

PROFILE = dict(workspace='分布式训练空间', project='test', group='test-H200', quota='1,20,200', image='test:cuda')


def spec():
    return JobSpec(run_id='equilibration-test-000001', request_id='test-request-000001',
        project_id='test', route='library-first', engine='library-first-openmm-equilibration',
        parent_run_id='qc-parent-test-000001', command=['unused'], working_directory='/tmp',
        output_directory='/tmp/unused', execution_mode='execute', human_approved=True,
        approval_note='Synthetic software test only; no compute authorization',
        resource_budget=ResourceBudget(max_runtime_minutes=120, gpu_count=1, cpu_count=20, memory_gb=200, max_candidates=1),
        parameters=dict(workflow_id='ligand-flow-' + 'a'*24, short_qc_run_id='qc-parent-test-000001',
            platform='CUDA', cpu_threads=4, duration_ps=500., report_interval_ps=10.,
            platform_validation_required=True, scheduler_profile=PROFILE,
            platform_properties={'Precision':'mixed', 'DeviceIndex':'0', 'UseCpuPme':'false', 'DeterministicForces':'true'}),
        provenance={'worker_backend':'external-openmm'})


@pytest.mark.parametrize('field,value', [('platform','CPU'),('cpu_threads',20),('duration_ps',1000),
    ('platform_validation_required',False),('workflow_id','unbound')])
def test_boundary_rejects_unapproved_science_or_threads(field,value):
    s=spec(); ext.validate_boundary(s); s.parameters[field]=value
    with pytest.raises(ValueError,match='boundary'): ext.validate_boundary(s)


@pytest.mark.parametrize('changes', [{'gpu_count':2},{'cpu_count':4},{'memory_gb':32},
    {'max_candidates':2},{'max_runtime_minutes':121}])
def test_exact_allocated_quota(changes):
    s=spec(); s.resource_budget=s.resource_budget.model_copy(update=changes)
    with pytest.raises(ValueError,match='allocated quota'): ext.validate_boundary(s)


def test_reserve_is_exclusive_and_retains_terminal_failure(tmp_path,monkeypatch):
    s=spec(); r=RunResult(run_id=s.run_id,project_id=s.project_id,engine=s.engine,route=s.route,
        attempt=s.attempt,parent_run_id=s.parent_run_id,state='QUEUED',submitted_at=utc_now(),
        message='Synthetic queued record', stdout_path='stdout.log', stderr_path='stderr.log')
    from test_external_hpc import approved
    key=tmp_path/'test-only.key'; key.write_bytes(b'isolated-software-test-key-not-production'); key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE',str(key))
    registry=ArtifactRegistry(tmp_path); registry.create(approved(s),r)
    original=ext.inspect(tmp_path,s.run_id)
    ext.reserve(tmp_path,s.run_id,original['spec_sha256'])
    with pytest.raises(ValueError,match='unreserved'): ext.reserve(tmp_path,s.run_id,original['spec_sha256'])
    with pytest.raises(ValueError,match='Exact reserved'): ext.fail(tmp_path,s.run_id,'wrong',original['name'],'FAILED')
    ext.fail(tmp_path,s.run_id,original['spec_sha256'],original['name'],'TIMED_OUT')
    assert registry.load_result(s.run_id).state=='TIMED_OUT'
    with pytest.raises(ValueError,match='already started'): ext.run(tmp_path,s.run_id,original['spec_sha256'])
    changed=registry.load_spec(s.run_id)
    changed.parameters['scheduler_profile']['image']='unapproved:image'
    registry.write_spec(changed)
    with pytest.raises(ValueError,match='Approved job changed'): ext.inspect(tmp_path,s.run_id)


def test_dispatch_uses_frozen_profile_and_never_rounds_quota(monkeypatch):
    folder=Path(__file__).resolve().parents[2]/'routes/ligand-first/ops'
    monkeypatch.syspath_prepend(str(folder))
    module=importlib.util.spec_from_file_location('test_openmm_dispatch',folder/'dispatch_ligand_openmm.py')
    driver=importlib.util.module_from_spec(module); module.loader.exec_module(driver)
    s=spec()
    record=dict(run_id=s.run_id,spec_sha256='a'*64,name='test-equil',minutes=120,engine_cpu_threads=4,
        budget=s.resource_budget.model_dump(),scheduler_profile=PROFILE)
    args=driver.create_args(record)
    for key in PROFILE: assert args[args.index('--'+key)+1]==PROFILE[key]
    assert args[args.index('--max-time')+1]=='2.0'
    assert '--no-auto-fault-tolerance' in args
    assert 'ligand_external_openmm.py' in args[-1]
    record['budget']['memory_gb']=201
    with pytest.raises(ValueError,match='allocated quota'): driver.create_args(record)


def test_ligand_override_does_not_change_library_first(tmp_path):
    from covscreen.execution import JobManager
    manager=JobManager(tmp_path)
    manager.config={'openmm_equilibration':{'platform':'CPU'},
                    'ligand_openmm_equilibration':{'platform':'CUDA'}}
    assert manager.equilibration_runtime({'workflow_id':'ligand-flow-'+ 'a'*24})['platform']=='CUDA'
    assert manager.equilibration_runtime({'workflow_id':'library-flow-'+ 'a'*24})['platform']=='CPU'
    assert manager.equilibration_runtime({})['platform']=='CPU'
