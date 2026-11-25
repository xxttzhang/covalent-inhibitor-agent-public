import hashlib
import json
from pathlib import Path

import pytest

from covscreen_library_first.md_equilibration import run_openmm_equilibration


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_rejects_nonpassing_short_qc_before_openmm_import(tmp_path):
    parameter = tmp_path/"parameter.json"
    parameter.write_text(json.dumps({"status": "COVALENT_PARAMETERIZATION_AUDITED",
        "system_id": "x", "total_atoms": 2}))
    minimization = tmp_path/"minimization"; minimization.mkdir()
    minimization_manifest = minimization/"minimization.json"
    minimization_manifest.write_text(json.dumps({"status":
        "BOUNDED_MINIMIZATION_PASSED_SHORT_QC_PENDING", "system_id": "x"}))
    short = tmp_path/"short"; short.mkdir()
    short_manifest = short/"short-qc.json"
    short_manifest.write_text(json.dumps({"schema": "openmm-short-qc-artifact-v1",
        "status": "FAILED", "system_id": "x", "atoms": 2, "short_qc_completed": False}))
    with pytest.raises(ValueError, match="parent chain is inconsistent"):
        run_openmm_equilibration(parameter, sha(parameter), minimization,
            sha(minimization_manifest), short, sha(short_manifest), tmp_path/"output")
    assert not (tmp_path/"output").exists()


def test_actual_toy_npt_reports_progress_and_does_not_claim_admission(tmp_path, capsys):
    """Two-particle software test, never evidence for candidate 066."""
    np = pytest.importorskip('numpy')
    mm = pytest.importorskip('openmm')
    parameter = tmp_path/'parameter.json'
    parameter.write_text(json.dumps({'status':'COVALENT_PARAMETERIZATION_AUDITED',
        'system_id':'toy-npt', 'total_atoms':2, 'covalent_bond':{'atom_indices_1based':[1,2]}}))
    minimized = tmp_path/'minimized'; minimized.mkdir()
    short = tmp_path/'short'; short.mkdir()
    system = mm.System(); system.addParticle(32); system.addParticle(12)
    system.addConstraint(0, 1, 0.18)
    system.setDefaultPeriodicBoxVectors(mm.Vec3(.5,0,0),mm.Vec3(0,.5,0),mm.Vec3(0,0,.5))
    nonbonded = mm.NonbondedForce(); nonbonded.setNonbondedMethod(mm.NonbondedForce.PME)
    nonbonded.setCutoffDistance(.2)
    nonbonded.addParticle(.1,.1,0); nonbonded.addParticle(-.1,.1,0)
    system.addForce(nonbonded)
    system_path = minimized/'system.xml'; system_path.write_text(mm.XmlSerializer.serialize(system))
    minimization = minimized/'minimization.json'
    minimization.write_text(json.dumps({'status':'BOUNDED_MINIMIZATION_PASSED_SHORT_QC_PENDING',
        'system_id':'toy-npt','artifacts':{'system.xml':sha(system_path)}}))
    np.save(short/'final-positions-nm.npy',np.array([[.1,.1,.1],[.28,.1,.1]]))
    np.save(short/'final-velocities-nm-ps.npy',np.array([[.3,.3,.3],[-.3,-.3,-.3]]))
    qc = short/'short-qc.json'
    qc.write_text(json.dumps({'schema':'openmm-short-qc-artifact-v1',
        'status':'UNRESTRAINED_SHORT_QC_PASSED_EQUILIBRATION_PENDING',
        'short_qc_completed':True,'integration_stability_passed':True,'equilibration_completed':False,
        'system_id':'toy-npt','atoms':2,'parents':{'parameter_audit_sha256':sha(parameter),
        'minimization_manifest_sha256':sha(minimization)},'artifacts':{
        p.name:sha(p) for p in short.glob('*.npy')}}))
    result = run_openmm_equilibration(parameter,sha(parameter),minimized,sha(minimization),
        short,sha(qc),tmp_path/'output',duration_ps=.02,report_interval_ps=.01,
        platform_properties={'Threads':'1'},barostat_interval=1)
    assert result['equilibration_protocol_completed']
    assert not result['production_review_ready'] and not result['equilibrium_convergence_claimed']
    messages = [json.loads(line)['progress'] for line in capsys.readouterr().out.splitlines()]
    assert [m['stage'] for m in messages][:3] == ['loading_state','creating_context','initializing_state']
    assert [m['completed_steps'] for m in messages if m['stage']=='integrating']==[0,5,10]
    assert messages[-1]['stage']=='completed'
    assert json.loads((tmp_path/'output/progress.json').read_text())==messages[-1]
    rows = (tmp_path/'output/frames.jsonl').read_text().splitlines()
    assert len(rows)==2 and json.loads(rows[-1])==result['last_frame']
    assert (tmp_path/'output/checkpoint.chk').stat().st_size > 0
