import hashlib
import json
from pathlib import Path

import pytest

from covscreen_library_first.md_production import run_openmm_production


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_rejects_diagnostic_equilibration_before_openmm_import(tmp_path):
    parameter=tmp_path/"p.json"; parameter.write_text(json.dumps({"status":
        "COVALENT_PARAMETERIZATION_AUDITED","system_id":"x","total_atoms":2}))
    conversion=tmp_path/"c"; conversion.mkdir(); cm=conversion/"conversion.json"
    cm.write_text(json.dumps({"status":"conversion_written_forcefield_audit_pending","system_id":"x"}))
    minimum=tmp_path/"m"; minimum.mkdir(); mm=minimum/"minimization.json"
    mm.write_text(json.dumps({"status":"BOUNDED_MINIMIZATION_PASSED_SHORT_QC_PENDING","system_id":"x"}))
    eq=tmp_path/"e"; eq.mkdir(); em=eq/"equilibration.json"
    em.write_text(json.dumps({"schema":"openmm-equilibration-artifact-v1",
        "status":"EQUILIBRATION_DIAGNOSTIC_COMPLETE_NOT_ADMITTED","system_id":"x",
        "production_review_ready":False,"parents":{"parameter_audit_sha256":sha(parameter),
        "minimization_manifest_sha256":sha(mm)}}))
    with pytest.raises(ValueError,match="not admitted"):
        run_openmm_production(parameter,sha(parameter),conversion,sha(cm),minimum,sha(mm),
            eq,sha(em),tmp_path/"out",replica=1,velocity_seed=1,integrator_seed=2,
            barostat_seed=3,platform_name="CPU",platform_properties={"Threads":"1"},
            duration_ps=10000,trajectory_interval_ps=10)
    assert not (tmp_path/"out").exists()
