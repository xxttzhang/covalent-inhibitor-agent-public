"""Bounded CPU minimization gate for an independently audited OpenMM system."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re

from .md_system_conversion import sha


def _regular(path, digest, label):
    path = Path(path)
    if (path.is_symlink() or not path.is_file()
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or sha(path) != digest):
        raise ValueError(label + " checksum mismatch")
    return path


def minimize_audited_openmm_system(parameter_audit, parameter_audit_sha256,
                                    conversion_directory, conversion_manifest_sha256,
                                    forcefield_audit_directory,
                                    forcefield_audit_manifest_sha256, output,
                                    cpu_threads=2, max_iterations=100,
                                    tolerance_kj_mol_nm=10.0):
    """Run a small deterministic gate; do not claim equilibration or MD readiness."""
    parameter_path = _regular(parameter_audit, parameter_audit_sha256, "Parameter audit")
    parameter = json.loads(parameter_path.read_text())
    conversion_directory = Path(conversion_directory)
    forcefield_audit_directory = Path(forcefield_audit_directory)
    conversion_path = _regular(conversion_directory / "conversion.json",
        conversion_manifest_sha256, "Conversion manifest")
    forcefield_path = _regular(forcefield_audit_directory / "forcefield-audit.json",
        forcefield_audit_manifest_sha256, "Force-field audit manifest")
    conversion, forcefield = json.loads(conversion_path.read_text()), json.loads(forcefield_path.read_text())
    if (conversion.get("schema") != "openmm-conversion-artifact-v1"
            or conversion.get("status") != "conversion_written_forcefield_audit_pending"
            or forcefield.get("schema") != "openmm-forcefield-audit-artifact-v1"
            or forcefield.get("status") != "OPENMM_FORCEFIELD_AUDITED_MINIMIZATION_PENDING"
            or forcefield.get("independent_forcefield_audit_completed") is not True
            or forcefield.get("conversion_manifest_sha256") != conversion_manifest_sha256
            or forcefield.get("parameter_audit_sha256") != parameter_audit_sha256
            or conversion.get("parameter_audit_sha256") != parameter_audit_sha256
            or forcefield.get("system_id") != parameter.get("system_id")
            or conversion.get("system_id") != parameter.get("system_id")
            or forcefield.get("atoms") != parameter.get("total_atoms")
            or forcefield.get("simulation_started") is not False):
        raise ValueError("Minimization parent chain is inconsistent")
    arrays_path = _regular(conversion_directory / "source-atom-arrays.npz",
        conversion["artifacts"]["source-atom-arrays.npz"], "Source atom arrays")
    system_path = _regular(forcefield_audit_directory / "system.xml",
        forcefield["system_xml_sha256"], "Audited OpenMM system")
    if (type(cpu_threads) is not int or not 1 <= cpu_threads <= 4
            or type(max_iterations) is not int or not 1 <= max_iterations <= 1000
            or type(tolerance_kj_mol_nm) not in (int, float)
            or not math.isfinite(tolerance_kj_mol_nm)
            or not 0 < tolerance_kj_mol_nm <= 100):
        raise ValueError("Minimization resource or convergence bound is invalid")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Minimization output must be a new directory")

    import numpy as np
    import openmm as mm
    from openmm import unit

    arrays = np.load(arrays_path)
    positions = arrays["positions_nm"].copy()
    box = arrays["box_nm"].copy()
    if (positions.shape != (parameter["total_atoms"], 3)
            or box.shape != (3, 3) or not np.isfinite(positions).all()
            or not np.isfinite(box).all()):
        raise ValueError("Source coordinates or periodic box are invalid")
    system = mm.XmlSerializer.deserialize(system_path.read_text())
    if system.getNumParticles() != parameter["total_atoms"]:
        raise ValueError("Audited OpenMM particle count differs from parameter audit")
    system.setDefaultPeriodicBoxVectors(*[mm.Vec3(*vector) * unit.nanometer for vector in box])
    long_range = 0
    for force in system.getForces():
        if isinstance(force, mm.CustomNonbondedForce):
            force.setUseLongRangeCorrection(True)
            long_range += 1
    if long_range != 1:
        raise ValueError("Expected exactly one custom nonbonded force")
    output.mkdir(parents=True, exist_ok=False)
    integrator = mm.VerletIntegrator(0.001)
    integrator.setConstraintTolerance(1e-6)
    context = mm.Context(system, integrator, mm.Platform.getPlatformByName("CPU"),
                         {"Threads": str(cpu_threads)})
    try:
        context.setPositions(positions * unit.nanometer)
        context.applyConstraints(1e-6)
        initial = context.getState(getEnergy=True, getForces=True)
        initial_energy = float(initial.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole))
        initial_forces = initial.getForces(asNumpy=True).value_in_unit(
            unit.kilojoule_per_mole / unit.nanometer)
        if not math.isfinite(initial_energy) or not np.isfinite(initial_forces).all():
            raise ValueError("Initial OpenMM energy or forces are non-finite")
        mm.LocalEnergyMinimizer.minimize(context, float(tolerance_kj_mol_nm), max_iterations)
        state = context.getState(getPositions=True, getEnergy=True, getForces=True)
        coordinates = state.getPositions(asNumpy=True).value_in_unit(unit.nanometer)
        forces = state.getForces(asNumpy=True).value_in_unit(
            unit.kilojoule_per_mole / unit.nanometer)
        final_energy = float(state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole))
    finally:
        del context, integrator
    if (not np.isfinite(coordinates).all() or not np.isfinite(forces).all()
            or not math.isfinite(final_energy) or final_energy >= initial_energy):
        raise ValueError("Bounded minimization did not produce lower finite energy and forces")
    pair = parameter.get("covalent_bond", {}).get("atom_indices_1based")
    if (not isinstance(pair, list) or len(pair) != 2
            or any(type(index) is not int or not 1 <= index <= len(coordinates) for index in pair)):
        raise ValueError("Parameter audit lacks the covalent atom pair")
    covalent_distance = float(np.linalg.norm(coordinates[pair[0] - 1] - coordinates[pair[1] - 1]) * 10)
    if not 1.5 < covalent_distance < 2.2:
        raise ValueError("Covalent S-C distance left the bounded structural range")
    displacement = np.linalg.norm(coordinates - positions, axis=1) * 10
    np.save(output / "minimized-positions-nm.npy", coordinates)
    np.save(output / "cpu-reference-forces.npy", forces)
    (output / "system.xml").write_text(mm.XmlSerializer.serialize(system))
    result = {"schema": "openmm-minimization-artifact-v1",
        "status": "BOUNDED_MINIMIZATION_PASSED_SHORT_QC_PENDING",
        "system_id": parameter["system_id"], "atoms": len(coordinates),
        "parents": {"parameter_audit_sha256": parameter_audit_sha256,
            "conversion_manifest_sha256": conversion_manifest_sha256,
            "forcefield_audit_manifest_sha256": forcefield_audit_manifest_sha256},
        "platform": "CPU", "openmm_version": mm.__version__, "cpu_threads": cpu_threads,
        "constraint_tolerance": 1e-6, "lj_long_range_correction": True,
        "max_iterations": max_iterations, "tolerance_kj_mol_nm": float(tolerance_kj_mol_nm),
        "initial_energy_kj_mol": initial_energy, "final_energy_kj_mol": final_energy,
        "initial_force_rms_kj_mol_nm": float(np.sqrt(np.mean(initial_forces ** 2))),
        "final_force_rms_kj_mol_nm": float(np.sqrt(np.mean(forces ** 2))),
        "maximum_atom_displacement_angstrom": float(displacement.max()),
        "covalent_distance_angstrom": covalent_distance,
        "artifacts": {name: sha(output / name) for name in
            ("minimized-positions-nm.npy", "cpu-reference-forces.npy", "system.xml")},
        "convergence_claimed": False, "short_qc_completed": False,
        "equilibration_completed": False, "md_system_ready": False,
        "campaign_ready": False, "simulation_started": False}
    (output / "minimization.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result
