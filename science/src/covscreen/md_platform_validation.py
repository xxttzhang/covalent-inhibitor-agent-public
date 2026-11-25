"""Single-state numerical comparison before any CUDA integration."""
import math
import os
import hashlib
import json

POLICY = {'relative_energy_tolerance': 1e-5, 'relative_force_rms_tolerance': 1e-4}


def compare(cpu_energy, cuda_energy, cpu_forces, cuda_forces):
    import numpy as np
    cpu, cuda = np.asarray(cpu_forces), np.asarray(cuda_forces)
    if (cpu.ndim != 2 or cpu.shape[1] != 3 or cuda.shape != cpu.shape or not len(cpu)
            or not np.isfinite(cpu).all() or not np.isfinite(cuda).all()
            or not math.isfinite(cpu_energy) or not math.isfinite(cuda_energy)):
        raise ValueError('Platform comparison contains missing or nonfinite state')
    energy = abs(cuda_energy - cpu_energy)
    force = float(np.sqrt(np.mean((cuda - cpu)**2)))
    relative_energy = energy / max(1.0, abs(cpu_energy))
    relative_force = force / max(1.0, float(np.sqrt(np.mean(cpu**2))))
    return {'cpu_energy_kj_mol': float(cpu_energy), 'cuda_energy_kj_mol': float(cuda_energy),
            'energy_difference_kj_mol': float(energy), 'relative_energy_difference': relative_energy,
            'force_rms_difference_kj_mol_nm': force, 'relative_force_rms_difference': relative_force,
            'force_max_difference_kj_mol_nm': float(np.max(np.abs(cuda - cpu))),
            'relative_scale_floor': 1.0, 'thresholds': dict(POLICY),
            'passed': relative_energy <= POLICY['relative_energy_tolerance']
                      and relative_force <= POLICY['relative_force_rms_tolerance']}


def validate(system, positions, properties, system_id, parents, output):
    """Evaluate the identical frozen coordinates on CPU and CUDA; zero MD steps."""
    import numpy as np
    import openmm as mm
    from openmm import unit
    threads = int(os.environ.get('OPENMM_CPU_THREADS', '0'))
    if not 1 <= threads <= 8:
        raise ValueError('Platform comparison requires the bounded CPU environment')
    samples = []
    for name, settings in (('CPU', {'Threads': str(threads)}), ('CUDA', properties)):
        integrator = mm.VerletIntegrator(.001)
        context = mm.Context(system, integrator, mm.Platform.getPlatformByName(name), settings)
        try:
            if context.getPlatform().getName() != name:
                raise ValueError('Unexpected platform fallback')
            context.setPositions(positions * unit.nanometer)
            state = context.getState(getEnergy=True, getForces=True)
            energy = float(state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole))
            forces = np.array(state.getForces(asNumpy=True).value_in_unit(
                unit.kilojoule_per_mole / unit.nanometer), copy=True)
            actual = {key: context.getPlatform().getPropertyValue(context, key) for key in settings}
            samples.append((energy, forces, actual))
        finally:
            del context, integrator
    metrics = compare(samples[0][0], samples[1][0], samples[0][1], samples[1][1])
    artifacts = {}
    for label, sample in zip(('cpu', 'cuda'), samples):
        path = output / ('validation-' + label + '-forces.npy')
        np.save(path, sample[1])
        artifacts[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {'schema': 'openmm-platform-validation-v1', 'system_id': system_id,
            'parents': parents, 'atoms': len(positions), 'integration_steps': 0,
            'platforms': ['CPU', 'CUDA'], 'cpu_properties': samples[0][2],
            'cuda_properties': samples[1][2], 'requested_cuda_properties': dict(properties),
            'openmm_version': mm.__version__, 'metrics': metrics, 'artifacts': artifacts,
            'passed': metrics['passed'], 'scientific_scope': 'Numerical consistency at one frozen state only'}


def readback(folder, system_id, parents, properties, threads, atoms):
    """Recompute the comparison from hash-bound arrays without running a context."""
    import numpy as np
    value = json.loads((folder / 'platform-validation.json').read_text())
    if (value.get('schema') != 'openmm-platform-validation-v1'
            or value.get('system_id') != system_id or value.get('parents') != parents
            or value.get('integration_steps') != 0 or value.get('atoms') != atoms
            or value.get('platforms') != ['CPU', 'CUDA'] or value.get('passed') is not True
            or value.get('cpu_properties') != {'Threads': str(threads)}
            or value.get('requested_cuda_properties') != properties
            or value.get('cuda_properties') != properties):
        raise ValueError('Platform validation differs from its frozen inputs or properties')
    names = ('validation-cpu-forces.npy', 'validation-cuda-forces.npy')
    if set(value.get('artifacts', {})) != set(names):
        raise ValueError('Platform validation force inventory is incomplete')
    arrays = []
    for name in names:
        path = folder / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != value['artifacts'][name]:
            raise ValueError('Platform validation forces changed')
        forces = np.load(path, allow_pickle=False)
        if forces.shape != (atoms, 3):
            raise ValueError('Platform validation force shape differs from the system')
        arrays.append(forces)
    metrics = value.get('metrics', {})
    expected = compare(metrics.get('cpu_energy_kj_mol', float('nan')),
        metrics.get('cuda_energy_kj_mol', float('nan')), *arrays)
    if not expected['passed'] or set(metrics) != set(expected):
        raise ValueError('Platform validation failed the frozen numerical thresholds')
    for key, number in expected.items():
        if isinstance(number, float):
            if type(metrics[key]) not in (int, float) or not math.isclose(metrics[key], number, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError('Platform comparison contradicts its force arrays: ' + key)
        elif metrics[key] != number:
            raise ValueError('Platform comparison policy or flags changed')
    return value
