"""Projected minimizer for the explicit v2-local-bounded receptor policy.

Each bounded atom has a Euclidean displacement ball (not Cartesian box limits).
Every objective evaluation is feasible. Convergence is numerical stationarity,
never receptor geometry, environment, or scientific acceptance. Callers supply
coordinates in nm, energy in kJ/mol and gradients in kJ/mol/nm.
"""
from __future__ import annotations

import math
import numpy as np


def minimize_bounded(objective, initial_nm, reference_nm, radii_nm, fixed_indices, *,
                     max_iterations=1000, max_evaluations=10000,
                     tolerance=1e-3, initial_step=1e-4, max_step=1.,
                     residual_step=1e-3, max_backtracks=40):
    """Projected gradient with monotone Armijo search and safeguarded BB steps.

    radii_nm maps atom indices to strictly positive radii. Other nonfixed atoms
    are unbounded. Fixed coordinates must initially equal their references.
    Return the last feasible accepted state on budget or line-search exhaustion;
    only status=converged indicates the projected residual reached tolerance.
    No objective evaluation on an infeasible initial state is allowed.
    """
    x = np.array(initial_nm, dtype=float, copy=True)
    ref = np.array(reference_nm, dtype=float, copy=True)
    if x.ndim != 2 or x.shape[1] != 3 or not len(x) or ref.shape != x.shape:
        raise ValueError('Coordinates must be matching nonempty N by 3 arrays')
    if not np.isfinite(x).all() or not np.isfinite(ref).all():
        raise ValueError('Coordinates must be finite')
    fixed = list(fixed_indices)
    indices = list(radii_nm)
    if (len(set(fixed)) != len(fixed) or any(type(i) is not int or not 0 <= i < len(x)
            for i in fixed + indices) or set(fixed) & set(indices)):
        raise ValueError('Invalid, duplicate or overlapping atom indices')
    radii = np.array([radii_nm[i] for i in indices], dtype=float)
    if not np.isfinite(radii).all() or np.any(radii <= 0):
        raise ValueError('Ball radii must be finite and positive')
    margins = 64 * np.finfo(float).eps * np.maximum(1., np.max(np.abs(ref[indices]), axis=1))
    if np.any(radii <= margins):
        raise ValueError('Ball radii are smaller than coordinate precision permits')
    for value in (max_iterations, max_evaluations, max_backtracks):
        if type(value) is not int or value < 1:
            raise ValueError('Iteration and evaluation budgets must be positive integers')
    if any(not math.isfinite(v) or v <= 0 for v in (tolerance, initial_step, max_step, residual_step)):
        raise ValueError('Optimizer scales and tolerance must be finite and positive')
    if initial_step > max_step:
        raise ValueError('Initial step exceeds maximum step')

    def feasible(point):
        return (np.isfinite(point).all() and np.array_equal(point[fixed], ref[fixed])
                and np.all(np.linalg.norm(point[indices] - ref[indices], axis=1) <= radii))

    if not feasible(x):
        raise ValueError('Initial coordinates violate fixed coordinates or displacement balls')

    def project(point):
        point = point.copy()
        point[fixed] = ref[fixed]
        delta = point[indices] - ref[indices]
        norms = np.linalg.norm(delta, axis=1)
        outside = norms > radii
        if np.any(outside):
            # A few ulps inward avoid emitting a rounded point outside its ball.
            target = radii[outside] - margins[outside]
            chosen = np.asarray(indices, dtype=int)[outside]
            point[chosen] = ref[chosen] + delta[outside] * (target / norms[outside])[:, None]
        # Adding a small radial displacement to a translated molecular frame
        # can round outward even after the radius was rounded inward. Move only
        # offending coordinates by representable ulps toward their reference.
        for _ in range(8):
            outside = np.linalg.norm(point[indices] - ref[indices], axis=1) > radii
            if not np.any(outside):
                break
            chosen = np.asarray(indices, dtype=int)[outside]
            point[chosen] = np.nextafter(point[chosen], ref[chosen])
        if not feasible(point):
            raise ValueError('Projection cannot represent a finite feasible coordinate')
        return point

    evaluations = 0

    def evaluate(point):
        nonlocal evaluations
        assert feasible(point)
        # A misbehaving oracle must not be able to mutate the accepted state.
        energy, gradient = objective(point.copy())
        evaluations += 1
        gradient = np.array(gradient, dtype=float, copy=True)
        if not np.isscalar(energy) or not math.isfinite(float(energy)):
            raise ValueError('Objective energy is not finite')
        if gradient.shape != x.shape or not np.isfinite(gradient).all():
            raise ValueError('Objective gradient has invalid shape or nonfinite values')
        return float(energy), gradient

    energy, gradient = evaluate(x)
    step = initial_step
    history = [energy]
    status = 'iteration_limit'
    iterations = 0
    for iteration in range(max_iterations + 1):
        mapping = (x - project(x - residual_step * gradient)) / residual_step
        residual = float(np.max(np.linalg.norm(mapping, axis=1)))
        if residual <= tolerance:
            status = 'converged'
            break
        if iteration == max_iterations:
            break
        if evaluations >= max_evaluations:
            status = 'evaluation_limit'
            break
        accepted = False
        for _ in range(max_backtracks):
            candidate = project(x - step * gradient)
            delta = candidate - x
            slope = float(np.sum(gradient * delta))
            if slope >= 0 or np.array_equal(candidate, x):
                status = 'line_search_stalled'
                break
            new_energy, new_gradient = evaluate(candidate)
            if new_energy <= energy + 1e-4 * slope:
                accepted = True
                break
            if evaluations >= max_evaluations:
                status = 'evaluation_limit'
                break
            step *= 0.5
        if not accepted:
            if status not in {'evaluation_limit', 'line_search_stalled'}:
                status = 'line_search_failed'
            break
        y = new_gradient - gradient
        curvature = float(np.sum(delta * y))
        if curvature > 0:
            step = min(max_step, max(1e-16, float(np.sum(delta * delta)) / curvature))
        else:
            step = min(max_step, step * 2)
        x, energy, gradient = candidate, new_energy, new_gradient
        iterations += 1
        history.append(energy)
    assert feasible(x)
    return {'status': status, 'positions_nm': x, 'energy_kj_mol': energy,
            'projected_gradient_max_kj_mol_nm': residual,
            'iterations': iterations, 'evaluations': evaluations,
            'accepted_energy_history_kj_mol': history,
            'scientific_acceptance': False}


def minimize_openmm_bounded(context, reference_nm, radii_nm, fixed_indices, **options):
    return _minimize_openmm(context, reference_nm, radii_nm, fixed_indices,
                            minimize_bounded, **options)


def _minimize_openmm(context, reference_nm, radii_nm, fixed_indices, minimizer, **options):
    """Adapter for an unconstrained, nonperiodic OpenMM context.

    Used only by the explicit bounded final-stage policy. The context is restored to its
    initial positions on error and to its last accepted positions on termination.
    Fixed masses must match exactly; holonomic constraints and virtual sites are
    rejected because coordinate projection alone would not preserve them.
    """
    from openmm import unit
    system = context.getSystem()
    fixed = list(fixed_indices)
    massless = [i for i in range(system.getNumParticles())
                if system.getParticleMass(i).value_in_unit(unit.dalton) == 0]
    if (system.getNumConstraints() or system.usesPeriodicBoundaryConditions()
            or any(system.isVirtualSite(i) for i in range(system.getNumParticles()))):
        raise ValueError('Bounded adapter requires nonperiodic particles without constraints or virtual sites')
    if sorted(fixed) != massless:
        raise ValueError('Fixed atom indices differ from zero-mass particles')
    initial = np.array(context.getState(getPositions=True).getPositions(asNumpy=True)
                       .value_in_unit(unit.nanometer), copy=True)

    def objective(point):
        context.setPositions(point * unit.nanometer)
        state = context.getState(getEnergy=True, getForces=True)
        energy = state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
        gradient = -state.getForces(asNumpy=True).value_in_unit(unit.kilojoule_per_mole/unit.nanometer)
        return energy, gradient

    try:
        result = minimizer(objective, initial, reference_nm, radii_nm, fixed, **options)
    except Exception:
        context.setPositions(initial * unit.nanometer)
        raise
    context.setPositions(result['positions_nm'] * unit.nanometer)
    return result


def validate_optimizer_receipt(receipt, policy):
    """Check numerical termination claims; never grant molecular acceptance."""
    required = {'status', 'energy_kj_mol', 'projected_gradient_max_kj_mol_nm',
                'iterations', 'evaluations', 'accepted_energy_history_kj_mol', 'scientific_acceptance'}
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise ValueError('Bounded optimizer receipt is missing or has unexpected fields')
    history = receipt['accepted_energy_history_kj_mol']
    iterations, evaluations = receipt['iterations'], receipt['evaluations']
    residual = receipt['projected_gradient_max_kj_mol_nm']
    if (receipt['status'] != 'converged' or receipt['scientific_acceptance'] is not False
            or type(iterations) is not int or not 0 <= iterations <= policy['max_iterations']
            or type(evaluations) is not int or not iterations + 1 <= evaluations <= policy['max_evaluations']
            or type(residual) not in (int, float) or not math.isfinite(residual)
            or not 0 <= residual <= policy['tolerance']
            or not isinstance(history, list) or len(history) != iterations + 1
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in history)
            or any(b > a for a, b in zip(history, history[1:]))
            or receipt['energy_kj_mol'] != history[-1]):
        raise ValueError('Bounded optimizer convergence or budget receipt is invalid')
