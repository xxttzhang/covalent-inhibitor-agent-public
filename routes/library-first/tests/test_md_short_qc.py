import hashlib
import json
from pathlib import Path

import pytest

from covscreen_library_first.md_short_qc import run_openmm_short_qc


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_rejects_parent_mismatch_before_openmm_import(tmp_path):
    parameter = tmp_path / "parameter.json"
    parameter.write_text(json.dumps({"status": "COVALENT_PARAMETERIZATION_AUDITED",
        "system_id": "candidate-1", "total_atoms": 2,
        "covalent_bond": {"atom_indices_1based": [1, 2]}}))
    conversion = tmp_path / "conversion"; conversion.mkdir()
    conversion_manifest = conversion / "conversion.json"
    conversion_manifest.write_text(json.dumps({"status": "conversion_written_forcefield_audit_pending",
        "system_id": "candidate-1", "parameter_audit_sha256": sha(parameter)}))
    forcefield = tmp_path / "forcefield"; forcefield.mkdir()
    forcefield_manifest = forcefield / "forcefield-audit.json"
    forcefield_manifest.write_text(json.dumps({"status": "OPENMM_FORCEFIELD_AUDITED_MINIMIZATION_PENDING",
        "system_id": "candidate-1", "parameter_audit_sha256": sha(parameter),
        "conversion_manifest_sha256": sha(conversion_manifest)}))
    minimization = tmp_path / "minimization"; minimization.mkdir()
    minimization_manifest = minimization / "minimization.json"
    minimization_manifest.write_text(json.dumps({"schema": "openmm-minimization-artifact-v1",
        "status": "wrong", "system_id": "candidate-1", "atoms": 2,
        "short_qc_completed": False, "simulation_started": False, "parents": {}}))
    with pytest.raises(ValueError, match="parent chain is inconsistent"):
        run_openmm_short_qc(parameter, sha(parameter), conversion, sha(conversion_manifest),
            forcefield, sha(forcefield_manifest), minimization, sha(minimization_manifest),
            tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_runs_bounded_unrestrained_integration_and_preserves_pending_state(tmp_path):
    np = pytest.importorskip("numpy")
    mm = pytest.importorskip("openmm")
    from openmm import unit

    parameter = tmp_path / "parameter.json"
    parameter.write_text(json.dumps({"status": "COVALENT_PARAMETERIZATION_AUDITED",
        "system_id": "candidate-1", "total_atoms": 2,
        "covalent_bond": {"atom_indices_1based": [1, 2]}}))
    conversion = tmp_path / "conversion"; conversion.mkdir()
    conversion_manifest = conversion / "conversion.json"
    conversion_manifest.write_text(json.dumps({"status": "conversion_written_forcefield_audit_pending",
        "system_id": "candidate-1", "parameter_audit_sha256": sha(parameter)}))
    forcefield = tmp_path / "forcefield"; forcefield.mkdir()
    forcefield_manifest = forcefield / "forcefield-audit.json"
    forcefield_manifest.write_text(json.dumps({"status": "OPENMM_FORCEFIELD_AUDITED_MINIMIZATION_PENDING",
        "system_id": "candidate-1", "parameter_audit_sha256": sha(parameter),
        "conversion_manifest_sha256": sha(conversion_manifest)}))
    minimization = tmp_path / "minimization"; minimization.mkdir()
    system = mm.System()
    system.addParticle(32.0); system.addParticle(12.0)
    bond = mm.HarmonicBondForce()
    bond.addBond(0, 1, 0.18, 100000.0)
    system.addForce(bond)
    system.setDefaultPeriodicBoxVectors(mm.Vec3(3, 0, 0), mm.Vec3(0, 3, 0), mm.Vec3(0, 0, 3))
    system_path = minimization / "system.xml"
    system_path.write_text(mm.XmlSerializer.serialize(system))
    positions = np.asarray([[0.0, 0.0, 0.0], [0.18, 0.0, 0.0]])
    positions_path = minimization / "minimized-positions-nm.npy"; np.save(positions_path, positions)
    integrator = mm.VerletIntegrator(0.001)
    context = mm.Context(system, integrator, mm.Platform.getPlatformByName("CPU"), {"Threads": "1"})
    context.setPositions(positions * unit.nanometer)
    state = context.getState(getEnergy=True, getForces=True)
    energy = float(state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole))
    forces = state.getForces(asNumpy=True).value_in_unit(unit.kilojoule_per_mole/unit.nanometer)
    del context, integrator
    forces_path = minimization / "cpu-reference-forces.npy"; np.save(forces_path, forces)
    minimization_manifest = minimization / "minimization.json"
    minimization_manifest.write_text(json.dumps({"schema": "openmm-minimization-artifact-v1",
        "status": "BOUNDED_MINIMIZATION_PASSED_SHORT_QC_PENDING", "system_id": "candidate-1",
        "atoms": 2, "short_qc_completed": False, "simulation_started": False,
        "final_energy_kj_mol": energy,
        "parents": {"parameter_audit_sha256": sha(parameter),
            "conversion_manifest_sha256": sha(conversion_manifest),
            "forcefield_audit_manifest_sha256": sha(forcefield_manifest)},
        "artifacts": {"system.xml": sha(system_path),
            "minimized-positions-nm.npy": sha(positions_path),
            "cpu-reference-forces.npy": sha(forces_path)}}))
    result = run_openmm_short_qc(parameter, sha(parameter), conversion,
        sha(conversion_manifest), forcefield, sha(forcefield_manifest), minimization,
        sha(minimization_manifest), tmp_path / "output", cpu_threads=1, steps=10,
        report_interval=5, velocity_seed=7)
    assert result["status"] == "UNRESTRAINED_SHORT_QC_PASSED_EQUILIBRATION_PENDING"
    assert result["short_qc_completed"] is True
    assert result["integration_stability_passed"] is True
    assert result["equilibration_completed"] is False
    assert result["md_system_ready"] is False
    assert result["frames"] == 2
    assert all((tmp_path / "output" / name).is_file() for name in
        ("short-qc.json", "frames.jsonl", "final-positions-nm.npy", "final-velocities-nm-ps.npy"))
