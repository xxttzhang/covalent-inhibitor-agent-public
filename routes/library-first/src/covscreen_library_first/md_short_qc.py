"""Bounded unrestrained OpenMM integration smoke gate after minimization."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .md_system_conversion import sha
from .md_system_minimization import _regular


def run_openmm_short_qc(parameter_audit, parameter_audit_sha256,
                        conversion_directory, conversion_manifest_sha256,
                        forcefield_audit_directory, forcefield_audit_manifest_sha256,
                        minimization_directory, minimization_manifest_sha256, output,
                        *, cpu_threads=2, steps=1000, report_interval=100,
                        timestep_fs=1.0, temperature_k=300.0, friction_per_ps=1.0,
                        velocity_seed=20261014):
    """Run an integration-stability gate; do not claim equilibration or MD readiness."""
    parameter_path = _regular(parameter_audit, parameter_audit_sha256, "Parameter audit")
    parameter = json.loads(parameter_path.read_text())
    conversion_directory = Path(conversion_directory)
    forcefield_audit_directory = Path(forcefield_audit_directory)
    minimization_directory = Path(minimization_directory)
    conversion_path = _regular(conversion_directory / "conversion.json",
        conversion_manifest_sha256, "Conversion manifest")
    forcefield_path = _regular(forcefield_audit_directory / "forcefield-audit.json",
        forcefield_audit_manifest_sha256, "Force-field audit manifest")
    minimization_path = _regular(minimization_directory / "minimization.json",
        minimization_manifest_sha256, "Minimization manifest")
    conversion = json.loads(conversion_path.read_text())
    forcefield = json.loads(forcefield_path.read_text())
    minimization = json.loads(minimization_path.read_text())
    if (parameter.get("status") != "COVALENT_PARAMETERIZATION_AUDITED"
            or conversion.get("status") != "conversion_written_forcefield_audit_pending"
            or forcefield.get("status") != "OPENMM_FORCEFIELD_AUDITED_MINIMIZATION_PENDING"
            or minimization.get("schema") != "openmm-minimization-artifact-v1"
            or minimization.get("status") != "BOUNDED_MINIMIZATION_PASSED_SHORT_QC_PENDING"
            or minimization.get("short_qc_completed") is not False
            or minimization.get("simulation_started") is not False
            or conversion.get("parameter_audit_sha256") != parameter_audit_sha256
            or forcefield.get("parameter_audit_sha256") != parameter_audit_sha256
            or forcefield.get("conversion_manifest_sha256") != conversion_manifest_sha256
            or minimization.get("parents") != {
                "parameter_audit_sha256": parameter_audit_sha256,
                "conversion_manifest_sha256": conversion_manifest_sha256,
                "forcefield_audit_manifest_sha256": forcefield_audit_manifest_sha256}
            or len({parameter.get("system_id"), conversion.get("system_id"),
                    forcefield.get("system_id"), minimization.get("system_id")}) != 1
            or minimization.get("atoms") != parameter.get("total_atoms")):
        raise ValueError("Short-QC parent chain is inconsistent")
    system_path = _regular(minimization_directory / "system.xml",
        minimization["artifacts"]["system.xml"], "Minimized OpenMM system")
    positions_path = _regular(minimization_directory / "minimized-positions-nm.npy",
        minimization["artifacts"]["minimized-positions-nm.npy"], "Minimized positions")
    reference_forces_path = _regular(minimization_directory / "cpu-reference-forces.npy",
        minimization["artifacts"]["cpu-reference-forces.npy"], "Reference forces")
    if (type(cpu_threads) is not int or not 1 <= cpu_threads <= 4
            or type(steps) is not int or not 10 <= steps <= 10000
            or type(report_interval) is not int or not 1 <= report_interval <= steps
            or steps % report_interval
            or type(velocity_seed) is not int or not 1 <= velocity_seed <= 2147483647
            or type(timestep_fs) not in (int, float) or not 0 < timestep_fs <= 2
            or type(temperature_k) not in (int, float) or not 10 <= temperature_k <= 400
            or type(friction_per_ps) not in (int, float) or not 0 < friction_per_ps <= 10):
        raise ValueError("Short-QC protocol or resource bound is invalid")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Short-QC output must be a new directory")

    import numpy as np
    import openmm as mm
    from openmm import unit

    positions = np.load(positions_path)
    reference_forces = np.load(reference_forces_path)
    system = mm.XmlSerializer.deserialize(system_path.read_text())
    if (positions.shape != (parameter["total_atoms"], 3)
            or reference_forces.shape != positions.shape
            or not np.isfinite(positions).all() or not np.isfinite(reference_forces).all()
            or system.getNumParticles() != len(positions)):
        raise ValueError("Short-QC OpenMM state is invalid")
    pair = parameter.get("covalent_bond", {}).get("atom_indices_1based")
    if (not isinstance(pair, list) or len(pair) != 2
            or any(type(index) is not int or not 1 <= index <= len(positions) for index in pair)):
        raise ValueError("Parameter audit lacks the covalent atom pair")
    output.mkdir(parents=True, exist_ok=False)
    integrator = mm.LangevinMiddleIntegrator(
        float(temperature_k) * unit.kelvin,
        float(friction_per_ps) / unit.picosecond,
        float(timestep_fs) * unit.femtosecond)
    integrator.setConstraintTolerance(1e-6)
    integrator.setRandomNumberSeed(velocity_seed)
    context = mm.Context(system, integrator, mm.Platform.getPlatformByName("CPU"),
                         {"Threads": str(cpu_threads)})
    rows = []
    try:
        context.setPositions(positions * unit.nanometer)
        context.applyConstraints(1e-6)
        replay = context.getState(getEnergy=True, getForces=True)
        replay_energy = float(replay.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole))
        replay_forces = replay.getForces(asNumpy=True).value_in_unit(
            unit.kilojoule_per_mole / unit.nanometer)
        energy_delta = abs(replay_energy - float(minimization["final_energy_kj_mol"]))
        force_scale = max(float(np.sqrt(np.mean(reference_forces ** 2))), 1.0)
        force_relative_rms = float(np.sqrt(np.mean((replay_forces-reference_forces) ** 2)) / force_scale)
        if (not math.isfinite(replay_energy) or not np.isfinite(replay_forces).all()
                or energy_delta > max(1.0, abs(replay_energy) * 1e-5)
                or force_relative_rms > 1e-5):
            raise ValueError("Minimized CPU energy/force state did not replay")
        context.setVelocitiesToTemperature(float(temperature_k) * unit.kelvin, velocity_seed)
        constraints = [system.getConstraintParameters(i) for i in range(system.getNumConstraints())]
        indices = np.asarray([(int(a), int(b)) for a, b, _ in constraints], dtype=int)
        lengths = np.asarray([length.value_in_unit(unit.nanometer) for _, _, length in constraints])
        has_com_remover = any(isinstance(force, mm.CMMotionRemover) for force in system.getForces())
        dof = 3 * len(positions) - len(constraints) - (3 if has_com_remover else 0)
        if dof <= 0:
            raise ValueError("Short-QC system has no positive degrees of freedom")
        completed_steps = 0
        for _ in range(steps // report_interval):
            integrator.step(report_interval)
            completed_steps += report_interval
            state = context.getState(getPositions=True, getVelocities=True, getEnergy=True)
            xyz = state.getPositions(asNumpy=True).value_in_unit(unit.nanometer)
            potential = float(state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole))
            kinetic = float(state.getKineticEnergy().value_in_unit(unit.kilojoule_per_mole))
            temperature = 2 * kinetic / (dof * unit.MOLAR_GAS_CONSTANT_R.value_in_unit(
                unit.kilojoule_per_mole / unit.kelvin))
            constraint_error = 0.0
            if len(indices):
                observed = np.linalg.norm(xyz[indices[:, 0]] - xyz[indices[:, 1]], axis=1)
                constraint_error = float(np.max(np.abs(observed-lengths) / lengths))
            distance = float(np.linalg.norm(xyz[pair[0]-1]-xyz[pair[1]-1]) * 10)
            if (not np.isfinite(xyz).all() or not all(map(math.isfinite,
                    (potential, kinetic, temperature, distance, constraint_error)))
                    or not 1.4 < distance < 2.4 or constraint_error >= 1e-4
                    or not 1 < temperature < 2000):
                raise ValueError("Short-QC integration stability gate failed")
            rows.append({"step": completed_steps,
                "time_ps": float(completed_steps * timestep_fs / 1000),
                "potential_energy_kj_mol": potential,
                "kinetic_energy_kj_mol": kinetic, "temperature_k": float(temperature),
                "covalent_distance_angstrom": distance,
                "max_relative_constraint_error": constraint_error})
        final = context.getState(getPositions=True, getVelocities=True)
        final_positions = final.getPositions(asNumpy=True).value_in_unit(unit.nanometer)
        final_velocities = final.getVelocities(asNumpy=True).value_in_unit(
            unit.nanometer / unit.picosecond)
    finally:
        del context, integrator
    np.save(output / "final-positions-nm.npy", final_positions)
    np.save(output / "final-velocities-nm-ps.npy", final_velocities)
    (output / "frames.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = {"schema": "openmm-short-qc-artifact-v1",
        "status": "UNRESTRAINED_SHORT_QC_PASSED_EQUILIBRATION_PENDING",
        "system_id": parameter["system_id"], "atoms": len(positions),
        "parents": {"parameter_audit_sha256": parameter_audit_sha256,
            "conversion_manifest_sha256": conversion_manifest_sha256,
            "forcefield_audit_manifest_sha256": forcefield_audit_manifest_sha256,
            "minimization_manifest_sha256": minimization_manifest_sha256},
        "protocol": {"ensemble": "NVT", "unrestrained": True, "platform": "CPU",
            "cpu_threads": cpu_threads, "steps": steps, "report_interval": report_interval,
            "timestep_fs": float(timestep_fs), "duration_ps": steps*timestep_fs/1000,
            "temperature_k": float(temperature_k), "friction_per_ps": float(friction_per_ps),
            "velocity_seed": velocity_seed, "constraint_tolerance": 1e-6},
        "state_replay": {"energy_difference_kj_mol": energy_delta,
            "relative_force_rms_difference": force_relative_rms},
        "frames": len(rows), "first_frame": rows[0], "last_frame": rows[-1],
        "artifacts": {name: sha(output / name) for name in
            ("final-positions-nm.npy", "final-velocities-nm-ps.npy", "frames.jsonl")},
        "short_qc_completed": True, "integration_stability_passed": True,
        "equilibration_completed": False, "production_started": False,
        "md_system_ready": False, "campaign_ready": False,
        "scientific_scope": "Integration stability only; not equilibration, binding stability, affinity, or kinetics"}
    (output / "short-qc.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result
