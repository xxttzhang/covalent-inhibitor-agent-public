"""Hash-bound unrestrained NPT equilibration after OpenMM short-QC."""
from __future__ import annotations

import json
import math
from pathlib import Path
import time

from .md_system_conversion import sha
from .md_system_minimization import _regular


def run_openmm_equilibration(parameter_audit, parameter_audit_sha256,
                             minimization_directory, minimization_manifest_sha256,
                             short_qc_directory, short_qc_manifest_sha256, output,
                             *, platform_name="CPU", platform_properties=None,
                             duration_ps=500.0, report_interval_ps=10.0,
                             timestep_fs=2.0, temperature_k=300.0,
                             pressure_bar=1.01325, friction_per_ps=1.0,
                             barostat_interval=25, integrator_seed=20261015,
                             barostat_seed=20261016, progress_chunk_steps=25):
    """Run NPT relaxation; protocol completion is not a convergence claim."""
    parameter_path = _regular(parameter_audit, parameter_audit_sha256, "Parameter audit")
    parameter = json.loads(parameter_path.read_text())
    minimization_directory = Path(minimization_directory)
    short_qc_directory = Path(short_qc_directory)
    minimization_path = _regular(minimization_directory / "minimization.json",
        minimization_manifest_sha256, "Minimization manifest")
    short_path = _regular(short_qc_directory / "short-qc.json",
        short_qc_manifest_sha256, "Short-QC manifest")
    minimization = json.loads(minimization_path.read_text())
    short_qc = json.loads(short_path.read_text())
    if (parameter.get("status") != "COVALENT_PARAMETERIZATION_AUDITED"
            or minimization.get("status") != "BOUNDED_MINIMIZATION_PASSED_SHORT_QC_PENDING"
            or short_qc.get("schema") != "openmm-short-qc-artifact-v1"
            or short_qc.get("status") != "UNRESTRAINED_SHORT_QC_PASSED_EQUILIBRATION_PENDING"
            or short_qc.get("short_qc_completed") is not True
            or short_qc.get("integration_stability_passed") is not True
            or short_qc.get("equilibration_completed") is not False
            or short_qc.get("parents", {}).get("parameter_audit_sha256") != parameter_audit_sha256
            or short_qc.get("parents", {}).get("minimization_manifest_sha256") != minimization_manifest_sha256
            or len({parameter.get("system_id"), minimization.get("system_id"),
                    short_qc.get("system_id")}) != 1
            or short_qc.get("atoms") != parameter.get("total_atoms")):
        raise ValueError("Equilibration parent chain is inconsistent")
    system_path = _regular(minimization_directory / "system.xml",
        minimization["artifacts"]["system.xml"], "Minimized OpenMM system")
    positions_path = _regular(short_qc_directory / "final-positions-nm.npy",
        short_qc["artifacts"]["final-positions-nm.npy"], "Short-QC positions")
    velocities_path = _regular(short_qc_directory / "final-velocities-nm-ps.npy",
        short_qc["artifacts"]["final-velocities-nm-ps.npy"], "Short-QC velocities")
    numeric = (duration_ps, report_interval_ps, timestep_fs, temperature_k,
               pressure_bar, friction_per_ps)
    if (platform_name not in {"CPU", "CUDA"}
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in numeric)
            or not 0.02 <= duration_ps <= 2000 or not 0.01 <= report_interval_ps <= duration_ps
            or not 0 < timestep_fs <= 2 or not 250 <= temperature_k <= 330
            or not 0.5 <= pressure_bar <= 2 or not 0 < friction_per_ps <= 10
            or type(barostat_interval) is not int or not 1 <= barostat_interval <= 100
            or type(progress_chunk_steps) is not int or not 1 <= progress_chunk_steps <= 100
            or any(type(seed) is not int or not 1 <= seed <= 2147483647
                   for seed in (integrator_seed, barostat_seed))):
        raise ValueError("Equilibration protocol or runtime is invalid")
    steps = round(duration_ps * 1000 / timestep_fs)
    report_steps = round(report_interval_ps * 1000 / timestep_fs)
    if (steps < 10 or report_steps < 1 or steps % report_steps
            or not math.isclose(steps*timestep_fs/1000, duration_ps, abs_tol=1e-12)
            or not math.isclose(report_steps*timestep_fs/1000, report_interval_ps, abs_tol=1e-12)):
        raise ValueError("Equilibration duration must divide exactly into report intervals")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Equilibration output must be a new directory")

    output.mkdir(parents=True, exist_ok=False)
    from covscreen.md_progress import MDProgress
    progress = MDProgress(output, parameter['system_id'], steps)
    progress.report('loading_state')

    import numpy as np
    import openmm as mm
    from openmm import unit

    positions, velocities = np.load(positions_path), np.load(velocities_path)
    system = mm.XmlSerializer.deserialize(system_path.read_text())
    if (positions.shape != (parameter["total_atoms"], 3) or velocities.shape != positions.shape
            or not np.isfinite(positions).all() or not np.isfinite(velocities).all()
            or system.getNumParticles() != len(positions)):
        raise ValueError("Equilibration OpenMM state is invalid")
    if any(isinstance(force, mm.MonteCarloBarostat) for force in system.getForces()):
        raise ValueError("Equilibration input unexpectedly contains a barostat")
    pair = parameter.get("covalent_bond", {}).get("atom_indices_1based")
    if (not isinstance(pair, list) or len(pair) != 2
            or any(type(index) is not int or not 1 <= index <= len(positions) for index in pair)):
        raise ValueError("Parameter audit lacks the covalent atom pair")
    properties = dict(platform_properties or {})
    validation = None
    if platform_name == 'CUDA':
        progress.report('validating_platforms')
        from covscreen.md_platform_validation import validate
        try:
            validation = validate(system, positions, properties, parameter['system_id'], {
                'parameter_audit_sha256': parameter_audit_sha256,
                'minimization_manifest_sha256': minimization_manifest_sha256,
                'short_qc_manifest_sha256': short_qc_manifest_sha256,
                'positions_sha256': short_qc['artifacts']['final-positions-nm.npy']}, output)
        except Exception as error:
            progress.report('failed', error='Platform validation failed before integration: ' + str(error))
            raise
        (output / 'platform-validation.json').write_text(json.dumps(validation, indent=2, allow_nan=False) + '\n')
        if not validation['passed']:
            progress.report('failed', error='CPU/CUDA numerical comparison failed before integration')
            raise ValueError('CPU/CUDA numerical comparison failed before integration')
    barostat = mm.MonteCarloBarostat(float(pressure_bar)*unit.bar,
        float(temperature_k)*unit.kelvin, barostat_interval)
    barostat.setRandomNumberSeed(barostat_seed)
    system.addForce(barostat)
    integrator = mm.LangevinMiddleIntegrator(float(temperature_k)*unit.kelvin,
        float(friction_per_ps)/unit.picosecond, float(timestep_fs)*unit.femtosecond)
    integrator.setConstraintTolerance(1e-6)
    integrator.setRandomNumberSeed(integrator_seed)
    platform = mm.Platform.getPlatformByName(platform_name)
    progress.report('creating_context')
    context = mm.Context(system, integrator, platform, properties)
    constraints = [system.getConstraintParameters(i) for i in range(system.getNumConstraints())]
    indices = np.asarray([(int(a), int(b)) for a, b, _ in constraints], dtype=int)
    lengths = np.asarray([length.value_in_unit(unit.nanometer) for _, _, length in constraints])
    has_com_remover = any(isinstance(force, mm.CMMotionRemover) for force in system.getForces())
    dof = 3*len(positions)-len(constraints)-(3 if has_com_remover else 0)
    total_mass_da = sum(system.getParticleMass(i).value_in_unit(unit.dalton)
                        for i in range(system.getNumParticles()))
    rows = []
    progress.report('initializing_state')
    completed = 0
    try:
        context.setPositions(positions*unit.nanometer)
        context.setVelocities(velocities*unit.nanometer/unit.picosecond)
        context.applyConstraints(1e-6); context.applyVelocityConstraints(1e-6)
        completed = 0
        progress.report('integrating')
        last_progress = time.monotonic()
        for _ in range(steps//report_steps):
            target_step = completed + report_steps
            while completed < target_step:
                count = min(progress_chunk_steps, target_step - completed)
                integrator.step(count); completed += count
                if completed < target_step and time.monotonic() - last_progress >= 10:
                    progress.report('integrating', completed)
                    last_progress = time.monotonic()
            state = context.getState(getPositions=True, getVelocities=True, getEnergy=True,
                                     enforcePeriodicBox=False)
            xyz = state.getPositions(asNumpy=True).value_in_unit(unit.nanometer)
            box = state.getPeriodicBoxVectors(asNumpy=True).value_in_unit(unit.nanometer)
            volume = float(abs(np.linalg.det(box)))
            potential = float(state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole))
            kinetic = float(state.getKineticEnergy().value_in_unit(unit.kilojoule_per_mole))
            temperature = 2*kinetic/(dof*unit.MOLAR_GAS_CONSTANT_R.value_in_unit(
                unit.kilojoule_per_mole/unit.kelvin))
            density = total_mass_da*1.66053906660/volume
            constraint_error = 0.0
            if len(indices):
                observed = np.linalg.norm(xyz[indices[:, 0]]-xyz[indices[:, 1]], axis=1)
                constraint_error = float(np.max(np.abs(observed-lengths)/lengths))
            distance = float(np.linalg.norm(xyz[pair[0]-1]-xyz[pair[1]-1])*10)
            values = (volume, potential, kinetic, temperature, density, constraint_error, distance)
            if (not np.isfinite(xyz).all() or not np.isfinite(box).all()
                    or not all(map(math.isfinite, values)) or volume <= 0
                    or not 1 < temperature < 2000 or not 100 < density < 2000
                    or constraint_error >= 1e-4 or not 1.4 < distance < 2.4):
                raise ValueError("Equilibration stability gate failed")
            rows.append({"step": completed, "time_ps": completed*timestep_fs/1000,
                "potential_energy_kj_mol": potential, "kinetic_energy_kj_mol": kinetic,
                "temperature_k": float(temperature), "volume_nm3": volume,
                "density_kg_m3": density, "covalent_distance_angstrom": distance,
                "max_relative_constraint_error": constraint_error})
            # Persist each valid sample, including partial evidence on timeout.
            with (output / "frames.jsonl").open("a") as stream:
                stream.write(json.dumps(rows[-1], allow_nan=False) + "\n")
                stream.flush()
            progress.report('integrating', completed, **rows[-1])
            last_progress = time.monotonic()
        final = context.getState(getPositions=True, getVelocities=True, getEnergy=True)
        final_positions = final.getPositions(asNumpy=True).value_in_unit(unit.nanometer)
        final_velocities = final.getVelocities(asNumpy=True).value_in_unit(
            unit.nanometer/unit.picosecond)
        final_box = final.getPeriodicBoxVectors(asNumpy=True).value_in_unit(unit.nanometer)
        final_state_xml = mm.XmlSerializer.serialize(final)
        checkpoint = context.createCheckpoint()
    except Exception as error:
        progress.report('failed', completed, error=str(error))
        raise
    finally:
        del context, integrator
    window = rows[-min(10, len(rows)):]
    from covscreen.md_equilibration_checks import equilibration_checks
    checks = equilibration_checks(window)
    admission_duration_met = duration_ps >= 500 and len(rows) >= 20
    stability_passed = all(checks.values())
    production_review_ready = admission_duration_met and stability_passed
    status = ("NPT_EQUILIBRATION_COMPLETED_PRODUCTION_REVIEW_PENDING"
              if production_review_ready else "EQUILIBRATION_DIAGNOSTIC_COMPLETE_NOT_ADMITTED")
    progress.report('writing_results', steps)
    np.save(output/"final-positions-nm.npy", final_positions)
    np.save(output/"final-velocities-nm-ps.npy", final_velocities)
    np.save(output/"final-box-vectors-nm.npy", final_box)
    (output/"final-state.xml").write_text(final_state_xml)
    (output/"checkpoint.chk").write_bytes(checkpoint)
    (output/"frames.jsonl").write_text("".join(json.dumps(row)+"\n" for row in rows))
    result = {"schema": "openmm-equilibration-artifact-v1", "status": status,
        "system_id": parameter["system_id"], "atoms": len(positions),
        "parents": {"parameter_audit_sha256": parameter_audit_sha256,
            "minimization_manifest_sha256": minimization_manifest_sha256,
            "short_qc_manifest_sha256": short_qc_manifest_sha256},
        "protocol": {"ensemble": "NPT", "unrestrained": True,
            "platform": platform_name, "platform_properties": properties,
            "duration_ps": float(duration_ps), "steps": steps,
            "report_interval_ps": float(report_interval_ps), "timestep_fs": float(timestep_fs),
            "temperature_k": float(temperature_k), "pressure_bar": float(pressure_bar),
            "friction_per_ps": float(friction_per_ps), "barostat_interval": barostat_interval,
            "integrator_seed": integrator_seed, "barostat_seed": barostat_seed},
        "frames": len(rows), "final_window_frames": len(window), "checks": checks,
        "last_frame": rows[-1], "admission_duration_met": admission_duration_met,
        "production_review_ready": production_review_ready,
        "equilibration_protocol_completed": True, "equilibrium_convergence_claimed": False,
        "production_started": False, "md_system_ready": False, "campaign_ready": False,
        "artifacts": {name: sha(output/name) for name in ("final-positions-nm.npy",
            "final-velocities-nm-ps.npy", "final-box-vectors-nm.npy", "final-state.xml",
            "checkpoint.chk", "frames.jsonl")},
        "scientific_scope": "NPT preparation and stability gate; not equilibrium convergence, affinity, or kinetics"}
    if validation is not None:
        result['artifacts']['platform-validation.json'] = sha(output / 'platform-validation.json')
        result['artifacts'].update(validation['artifacts'])
        result['platform_validation_passed'] = True
    result['observability'] = {'progress_chunk_steps': progress_chunk_steps,
        'progress_min_wall_seconds': 10, 'scientific_frame_interval_ps': float(report_interval_ps)}
    (output/"equilibration.json").write_text(json.dumps(result, indent=2, allow_nan=False)+"\n")
    progress.report('completed', steps, production_review_ready=production_review_ready)
    return result
