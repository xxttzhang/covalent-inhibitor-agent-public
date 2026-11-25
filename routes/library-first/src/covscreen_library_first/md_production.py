"""Hash-bound OpenMM production replica after admitted NPT equilibration."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .md_system_conversion import sha
from .md_system_minimization import _regular


def _run(parameter_audit, parameter_audit_sha256, conversion_directory,
         conversion_manifest_sha256, minimization_directory,
         minimization_manifest_sha256, equilibration_directory,
         equilibration_manifest_sha256, output, *, replica, velocity_seed,
         integrator_seed, barostat_seed, platform_name, platform_properties,
         duration_ps, trajectory_interval_ps, diagnostic):
    parameter_path = _regular(parameter_audit, parameter_audit_sha256, "Parameter audit")
    parameter = json.loads(parameter_path.read_text())
    conversion_directory, minimization_directory = Path(conversion_directory), Path(minimization_directory)
    equilibration_directory = Path(equilibration_directory)
    conversion_path = _regular(conversion_directory/"conversion.json",
        conversion_manifest_sha256, "Conversion manifest")
    minimization_path = _regular(minimization_directory/"minimization.json",
        minimization_manifest_sha256, "Minimization manifest")
    equilibration_path = _regular(equilibration_directory/"equilibration.json",
        equilibration_manifest_sha256, "Equilibration manifest")
    conversion, minimization = json.loads(conversion_path.read_text()), json.loads(minimization_path.read_text())
    equilibration = json.loads(equilibration_path.read_text())
    required_status = ("EQUILIBRATION_DIAGNOSTIC_COMPLETE_NOT_ADMITTED" if diagnostic
                       else "NPT_EQUILIBRATION_COMPLETED_PRODUCTION_REVIEW_PENDING")
    if (parameter.get("status") != "COVALENT_PARAMETERIZATION_AUDITED"
            or conversion.get("status") != "conversion_written_forcefield_audit_pending"
            or minimization.get("status") != "BOUNDED_MINIMIZATION_PASSED_SHORT_QC_PENDING"
            or equilibration.get("schema") != "openmm-equilibration-artifact-v1"
            or equilibration.get("status") != required_status
            or equilibration.get("parents", {}).get("parameter_audit_sha256") != parameter_audit_sha256
            or equilibration.get("parents", {}).get("minimization_manifest_sha256") != minimization_manifest_sha256
            or len({parameter.get("system_id"), conversion.get("system_id"),
                    minimization.get("system_id"), equilibration.get("system_id")}) != 1
            or (not diagnostic and (equilibration.get("production_review_ready") is not True
                                    or equilibration.get("admission_duration_met") is not True))
            or (diagnostic and equilibration.get("production_review_ready") is not False)):
        raise ValueError("Production parent chain is not admitted")
    topology_path = _regular(conversion_directory/"system.top",
        conversion["artifacts"]["system.top"], "Converted topology")
    system_path = _regular(minimization_directory/"system.xml",
        minimization["artifacts"]["system.xml"], "OpenMM system")
    positions_path = _regular(equilibration_directory/"final-positions-nm.npy",
        equilibration["artifacts"]["final-positions-nm.npy"], "Equilibrated positions")
    box_path = _regular(equilibration_directory/"final-box-vectors-nm.npy",
        equilibration["artifacts"]["final-box-vectors-nm.npy"], "Equilibrated box")
    seeds = (velocity_seed, integrator_seed, barostat_seed)
    if (type(replica) is not int or not 1 <= replica <= 3 or len(set(seeds)) != 3
            or any(type(seed) is not int or not 1 <= seed <= 2147483647 for seed in seeds)
            or platform_name not in {"CPU", "CUDA"}
            or not isinstance(platform_properties, dict)
            or any(not isinstance(k, str) or not isinstance(v, str)
                   for k, v in platform_properties.items())
            or type(duration_ps) not in (int, float) or not math.isfinite(duration_ps)
            or type(trajectory_interval_ps) not in (int, float)
            or not math.isfinite(trajectory_interval_ps)
            or (diagnostic and not 0.02 <= duration_ps <= 1)
            or (not diagnostic and duration_ps != 10000)
            or not 0.01 <= trajectory_interval_ps <= duration_ps):
        raise ValueError("Production protocol, seeds or runtime is invalid")
    timestep_fs = 2.0
    steps = round(duration_ps*1000/timestep_fs)
    report_steps = round(trajectory_interval_ps*1000/timestep_fs)
    if steps < 10 or report_steps < 1 or steps % report_steps:
        raise ValueError("Production duration must divide exactly into trajectory intervals")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Production output must be a new directory")

    import numpy as np
    import openmm as mm
    from openmm import app, unit

    positions, box = np.load(positions_path), np.load(box_path)
    system = mm.XmlSerializer.deserialize(system_path.read_text())
    if (positions.shape != (parameter["total_atoms"], 3) or box.shape != (3, 3)
            or not np.isfinite(positions).all() or not np.isfinite(box).all()
            or system.getNumParticles() != len(positions)
            or any(isinstance(force, mm.MonteCarloBarostat) for force in system.getForces())):
        raise ValueError("Production OpenMM state is invalid")
    pair = parameter.get("covalent_bond", {}).get("atom_indices_1based")
    if (not isinstance(pair, list) or len(pair) != 2
            or any(type(index) is not int or not 1 <= index <= len(positions) for index in pair)):
        raise ValueError("Parameter audit lacks the covalent atom pair")
    vectors = tuple(mm.Vec3(*row) for row in box)*unit.nanometer
    system.setDefaultPeriodicBoxVectors(*vectors)
    barostat = mm.MonteCarloBarostat(1.01325*unit.bar, 300*unit.kelvin, 25)
    barostat.setRandomNumberSeed(barostat_seed); system.addForce(barostat)
    integrator = mm.LangevinMiddleIntegrator(300*unit.kelvin, 1/unit.picosecond,
        timestep_fs*unit.femtosecond)
    integrator.setConstraintTolerance(1e-6); integrator.setRandomNumberSeed(integrator_seed)
    topology = app.GromacsTopFile(str(topology_path), periodicBoxVectors=vectors).topology
    if topology.getNumAtoms() != len(positions):
        raise ValueError("Production topology atom count mismatch")
    platform = mm.Platform.getPlatformByName(platform_name)
    output.mkdir(parents=True, exist_ok=False)
    simulation = app.Simulation(topology, system, integrator, platform, dict(platform_properties))
    simulation.context.setPositions(positions*unit.nanometer)
    simulation.context.applyConstraints(1e-6)
    simulation.context.setVelocitiesToTemperature(300*unit.kelvin, velocity_seed)
    simulation.reporters.append(app.DCDReporter(str(output/"trajectory.dcd"), report_steps,
                                                 enforcePeriodicBox=False))
    constraints = [system.getConstraintParameters(i) for i in range(system.getNumConstraints())]
    indices = np.asarray([(int(a), int(b)) for a, b, _ in constraints], dtype=int)
    lengths = np.asarray([length.value_in_unit(unit.nanometer) for _, _, length in constraints])
    has_com = any(isinstance(force, mm.CMMotionRemover) for force in system.getForces())
    dof = 3*len(positions)-len(constraints)-(3 if has_com else 0)
    total_mass = sum(system.getParticleMass(i).value_in_unit(unit.dalton)
                     for i in range(system.getNumParticles()))
    rows = []
    try:
        for _ in range(steps//report_steps):
            simulation.step(report_steps)
            state = simulation.context.getState(getPositions=True, getEnergy=True,
                enforcePeriodicBox=False)
            xyz = state.getPositions(asNumpy=True).value_in_unit(unit.nanometer)
            current_box = state.getPeriodicBoxVectors(asNumpy=True).value_in_unit(unit.nanometer)
            volume = float(abs(np.linalg.det(current_box)))
            potential = float(state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole))
            kinetic = float(state.getKineticEnergy().value_in_unit(unit.kilojoule_per_mole))
            temperature = 2*kinetic/(dof*unit.MOLAR_GAS_CONSTANT_R.value_in_unit(
                unit.kilojoule_per_mole/unit.kelvin))
            density = total_mass*1.66053906660/volume
            error = 0.0
            if len(indices):
                observed = np.linalg.norm(xyz[indices[:, 0]]-xyz[indices[:, 1]], axis=1)
                error = float(np.max(np.abs(observed-lengths)/lengths))
            distance = float(np.linalg.norm(xyz[pair[0]-1]-xyz[pair[1]-1])*10)
            values = (volume, potential, kinetic, temperature, density, error, distance)
            if (not np.isfinite(xyz).all() or not np.isfinite(current_box).all()
                    or not all(map(math.isfinite, values)) or not 1 < temperature < 2000
                    or not 100 < density < 2000 or error >= 1e-4 or not 1.4 < distance < 2.4):
                raise ValueError("Production integration stability gate failed")
            rows.append({"frame": len(rows)+1, "step": simulation.currentStep,
                "time_ps": simulation.currentStep*timestep_fs/1000,
                "potential_energy_kj_mol": potential, "kinetic_energy_kj_mol": kinetic,
                "temperature_k": float(temperature), "volume_nm3": volume,
                "density_kg_m3": density, "covalent_distance_angstrom": distance,
                "max_relative_constraint_error": error})
        final = simulation.context.getState(getPositions=True, getVelocities=True, getEnergy=True)
        final_positions = final.getPositions(asNumpy=True).value_in_unit(unit.nanometer)
        final_velocities = final.getVelocities(asNumpy=True).value_in_unit(unit.nanometer/unit.picosecond)
        final_box = final.getPeriodicBoxVectors(asNumpy=True).value_in_unit(unit.nanometer)
        final_state = mm.XmlSerializer.serialize(final)
        checkpoint = simulation.context.createCheckpoint()
    finally:
        del simulation, integrator
    np.save(output/"final-positions-nm.npy", final_positions)
    np.save(output/"final-velocities-nm-ps.npy", final_velocities)
    np.save(output/"final-box-vectors-nm.npy", final_box)
    (output/"final-state.xml").write_text(final_state)
    (output/"checkpoint.chk").write_bytes(checkpoint)
    (output/"frames.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
    status = ("PRODUCTION_ENGINE_DIAGNOSTIC_COMPLETE_NOT_SCIENTIFIC_TRAJECTORY" if diagnostic
              else "PRODUCTION_REPLICA_COMPLETE_TRAJECTORY_AUDIT_PENDING")
    result = {"schema": "openmm-production-replica-artifact-v1", "status": status,
        "system_id": parameter["system_id"], "replica": replica, "atoms": len(positions),
        "parents": {"parameter_audit_sha256": parameter_audit_sha256,
            "conversion_manifest_sha256": conversion_manifest_sha256,
            "minimization_manifest_sha256": minimization_manifest_sha256,
            "equilibration_manifest_sha256": equilibration_manifest_sha256},
        "protocol": {"ensemble": "NPT", "unrestrained": True, "platform": platform_name,
            "platform_properties": dict(platform_properties), "duration_ps": float(duration_ps),
            "timestep_fs": timestep_fs, "trajectory_interval_ps": float(trajectory_interval_ps),
            "frames": len(rows), "velocity_seed": velocity_seed,
            "integrator_seed": integrator_seed, "barostat_seed": barostat_seed},
        "last_frame": rows[-1], "production_completed": not diagnostic,
        "trajectory_audited": False, "analysis_completed": False,
        "binding_stability_claimed": False, "affinity_claimed": False,
        "artifacts": {name: sha(output/name) for name in ("trajectory.dcd",
            "final-positions-nm.npy", "final-velocities-nm-ps.npy",
            "final-box-vectors-nm.npy", "final-state.xml", "checkpoint.chk", "frames.jsonl")},
        "scientific_scope": "Finite production sampling; trajectory audit required, not affinity or kinetics"}
    (output/"production.json").write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")
    return result


def run_openmm_production(*args, **kwargs):
    return _run(*args, **kwargs, diagnostic=False)


def run_openmm_production_diagnostic(*args, **kwargs):
    """Exercise the engine on a non-admitted parent without creating production evidence."""
    return _run(*args, **kwargs, diagnostic=True)
