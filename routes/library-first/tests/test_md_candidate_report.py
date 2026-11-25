import hashlib
import json

from covscreen_library_first.md_candidate_report import build_candidate_md_report


def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def metric(median, p95, minimum=0.0, maximum=None):
    return {"median":median,"p95":p95,"min":minimum,"max":p95 if maximum is None else maximum}


def test_report_applies_three_replica_consensus_without_affinity_claim(tmp_path):
    summaries=[]
    for replica in (1,2,3):
        summaries.append({"replica":replica,"frames":1000,"metrics":{
            "protein_ca_rmsd_A":metric(2,3),"pocket_ca_rmsd_A":metric(1.5,2.5),
            "ligand_heavy_rmsd_A":metric(2,3),"ligand_com_displacement_A":metric(1,2),
            "contact_retention":metric(.6,.8,.4,.9),"covalent_distance_A":metric(1.8,1.9,1.7,2.0)}})
    analysis={"schema":"openmm-three-replica-structural-analysis-v1",
        "status":"THREE_REPLICA_STRUCTURAL_ANALYSIS_COMPLETE_REPORT_PENDING",
        "system_id":"candidate_001","replicas":[1,2,3],"descriptive_only":True,
        "replica_summaries":summaries}
    source=tmp_path/"structural-analysis.json"; source.write_text(json.dumps(analysis))
    result=build_candidate_md_report(source,sha(source),tmp_path/"out")
    assert result["classification"]=="STRUCTURALLY_SUPPORTIVE_FOR_EXPERT_REVIEW"
    assert result["supportive_replicas"]==3 and result["expert_review_required"] is True
    assert result["affinity_claimed"] is False


def test_report_rejects_short_replica_as_technical_failure(tmp_path):
    base={"protein_ca_rmsd_A":metric(2,3),"pocket_ca_rmsd_A":metric(1,2),
        "ligand_heavy_rmsd_A":metric(1,2),"ligand_com_displacement_A":metric(1,2),
        "contact_retention":metric(.5,.7),"covalent_distance_A":metric(1.8,1.9,1.7,2.0)}
    analysis={"schema":"openmm-three-replica-structural-analysis-v1",
        "status":"THREE_REPLICA_STRUCTURAL_ANALYSIS_COMPLETE_REPORT_PENDING",
        "system_id":"candidate_002","replicas":[1,2,3],"descriptive_only":True,
        "replica_summaries":[{"replica":i,"frames":999 if i==2 else 1000,"metrics":base} for i in (1,2,3)]}
    source=tmp_path/"analysis.json"; source.write_text(json.dumps(analysis))
    result=build_candidate_md_report(source,sha(source),tmp_path/"out")
    assert result["classification"]=="TECHNICAL_REJECTION"
