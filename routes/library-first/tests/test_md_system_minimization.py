import hashlib
import json
from pathlib import Path

import pytest

from covscreen_library_first.md_system_minimization import minimize_audited_openmm_system


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_rejects_parent_mismatch_before_openmm_import(tmp_path):
    parameter = tmp_path / "parameter.json"
    parameter.write_text(json.dumps({"system_id": "candidate-1", "total_atoms": 1}))
    conversion = tmp_path / "conversion"
    conversion.mkdir()
    conversion_manifest = conversion / "conversion.json"
    conversion_manifest.write_text(json.dumps({"schema": "openmm-conversion-artifact-v1",
        "status": "conversion_written_forcefield_audit_pending", "system_id": "candidate-1",
        "parameter_audit_sha256": sha(parameter), "artifacts": {}}))
    audit = tmp_path / "audit"
    audit.mkdir()
    audit_manifest = audit / "forcefield-audit.json"
    audit_manifest.write_text(json.dumps({"schema": "openmm-forcefield-audit-artifact-v1",
        "status": "OPENMM_FORCEFIELD_AUDITED_MINIMIZATION_PENDING", "system_id": "other",
        "atoms": 1, "parameter_audit_sha256": sha(parameter),
        "conversion_manifest_sha256": sha(conversion_manifest),
        "independent_forcefield_audit_completed": True, "simulation_started": False}))
    with pytest.raises(ValueError, match="parent chain is inconsistent"):
        minimize_audited_openmm_system(parameter, sha(parameter), conversion,
            sha(conversion_manifest), audit, sha(audit_manifest), tmp_path / "output")
    assert not (tmp_path / "output").exists()
