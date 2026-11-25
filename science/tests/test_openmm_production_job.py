import hashlib,json
from pathlib import Path
import sys
import pytest
from covscreen.jobs import JobSpec,ResourceBudget,RunResult,utc_now
from covscreen.native_approval import needs_approval
from covscreen.tool_api import library_first_run_openmm_production_tool
from test_openmm_equilibration_job import project as equilibrium_parents

WORKSPACE=Path(__file__).resolve().parents[2]
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def project(tmp_path):
    short_run,_=equilibrium_parents(tmp_path)
    config=tmp_path/'configs/local.inspire.yaml'
    config.write_text("openmm_production:\n  enabled: true\n"+f"  python: {sys.executable}\n"+f"  pythonpath: {sys.prefix}\n"+"  platform: CPU\n  platform_properties:\n    Threads: '1'\nruntime:\n  worker_backend: local\n")
    prep='openmm-preparation-parent-000001'; pf=tmp_path/'state/jobs'/prep/'artifacts/openmm-system-preparation'
    conv=pf/'conversion'; conv.mkdir(exist_ok=True); top=conv/'system.top'; top.write_text('top\n')
    cm=conv/'conversion.json'; cm.write_text(json.dumps({'status':'conversion_written_forcefield_audit_pending','system_id':'candidate_001','artifacts':{'system.top':sha(top)}})+'\n')
    minimum=pf/'minimization'; mm=minimum/'minimization.json'; md=json.loads(mm.read_text()); md['system_id']='candidate_001'; mm.write_text(json.dumps(md)+'\n')
    parameter=tmp_path/'state/jobs/parameterization-parent-000001/artifacts/adduct-parameterization/parameterization-audit.json'
    run='openmm-equilibration-parent-000001'; job=tmp_path/'state/jobs'/run; folder=job/'artifacts/openmm-equilibration'; folder.mkdir(parents=True)
    pos=folder/'final-positions-nm.npy'; pos.write_bytes(b'positions\n'); box=folder/'final-box-vectors-nm.npy'; box.write_bytes(b'box\n')
    manifest=folder/'equilibration.json'; manifest.write_text(json.dumps({'schema':'openmm-equilibration-artifact-v1','status':'NPT_EQUILIBRATION_COMPLETED_PRODUCTION_REVIEW_PENDING','system_id':'candidate_001','production_review_ready':True,'admission_duration_met':True,'production_started':False,'parents':{'parameter_audit_sha256':sha(parameter),'minimization_manifest_sha256':sha(mm)},'artifacts':{'final-positions-nm.npy':sha(pos),'final-box-vectors-nm.npy':sha(box)}})+'\n')
    spec=JobSpec(run_id=run,request_id='equilibrium-request-000001',project_id='candidate_001',route='library-first',engine='library-first-openmm-equilibration',command=['equilibrate'],working_directory=str(job),output_directory=str(folder),parent_run_id=short_run,resource_budget=ResourceBudget(max_runtime_minutes=720,cpu_count=4,memory_gb=32,max_candidates=1))
    result=RunResult(run_id=run,state='COMPLETED',engine='library-first-openmm-equilibration',route='library-first',project_id='candidate_001',submitted_at=utc_now(),started_at=utc_now(),finished_at=utc_now(),return_code=0,runtime_seconds=1,message='completed',stdout_path=str(job/'stdout.log'),stderr_path=str(job/'stderr.log'))
    (job/'spec.json').write_text(spec.model_dump_json()); (job/'result.json').write_text(result.model_dump_json())
    return run,manifest

def args(run,manifest): return {'action':'plan','equilibration_run_id':run,'equilibration_manifest_sha256':sha(manifest),'replica':2,'seed_base':100,'resource_budget':{'max_runtime_minutes':1440,'gpu_count':0,'cpu_count':4,'memory_gb':32,'max_candidates':1}}

def test_plan_freezes_admitted_equilibration_and_replica(tmp_path):
    run,manifest=project(tmp_path); result=library_first_run_openmm_production_tool(tmp_path,args(run,manifest)); spec=result['spec']
    assert result['status']=='PLANNED' and spec['engine']=='library-first-openmm-production'
    assert spec['parameters']['replica']==2 and [spec['parameters'][k] for k in ('velocity_seed','integrator_seed','barostat_seed')]==[106,107,108]
    assert spec['parameters']['duration_ps']==10000 and spec['provenance']['trajectory_audited'] is False
    assert len(spec['input_sha256'])==10 and not Path(spec['expected_output_paths'][0]).exists()

def test_plan_rejects_tampered_equilibration(tmp_path):
    run,manifest=project(tmp_path); (manifest.parent/'final-box-vectors-nm.npy').write_bytes(b'tampered')
    with pytest.raises(ValueError,match='production admission'): library_first_run_openmm_production_tool(tmp_path,args(run,manifest))

def test_only_start_requires_approval():
    name='covalent_library_first_run_openmm_production'
    assert needs_approval(name,{'action':'start'}) is True
    assert needs_approval(name,{'action':'plan'}) is False
