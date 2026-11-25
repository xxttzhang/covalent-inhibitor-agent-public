"""Free-coordinate warmup for the explicit v2-local-block policy.

Numerical convergence is not scientific acceptance. Every oracle call, including
phase restarts, counts against one budget. Molecular use needs a new approved plan.
"""
import numpy as np
from .receptor_bounded_minimizer import minimize_bounded


def minimize_free_then_bounded(objective, initial_nm, reference_nm, radii_nm,
                               fixed_indices, *, warmup_iterations=100,
                               max_iterations=1000, max_evaluations=3000, **options):
    if (type(warmup_iterations) is not int or warmup_iterations < 1
            or type(max_iterations) is not int or max_iterations <= warmup_iterations
            or type(max_evaluations) is not int or max_evaluations < 1):
        raise ValueError('Warmup must leave a positive joint iteration budget')
    fixed = list(fixed_indices)
    first = minimize_bounded(objective, initial_nm, reference_nm, radii_nm, fixed,
        max_iterations=1, max_evaluations=1, **options)
    phases = [dict(phase='initial_full_residual', **first)]
    calls, steps = first['evaluations'], 0
    current = first
    if first['status'] != 'converged' and calls < max_evaluations:
        x = first['positions_nm']
        held = sorted(set(fixed) | set(radii_nm))
        warm = minimize_bounded(objective, x, x, {}, held,
            max_iterations=warmup_iterations,
            max_evaluations=max_evaluations-calls, **options)
        phases.append(dict(phase='free_warmup', **warm))
        calls += warm['evaluations']; steps += warm['iterations']; current = warm
        # A planned warmup cap is a phase boundary, not a recovery attempt.
        # Numerical failure must terminate rather than silently trying joint mode.
        if warm['status'] in {'converged', 'iteration_limit'} and calls < max_evaluations:
            joint = minimize_bounded(objective, warm['positions_nm'], reference_nm,
                radii_nm, fixed, max_iterations=max_iterations-steps,
                max_evaluations=max_evaluations-calls, **options)
            phases.append(dict(phase='full_bounded', **joint))
            calls += joint['evaluations']; steps += joint['iterations']; current = joint
    # Free-block stationarity cannot certify the full system.
    full = phases[-1]['phase'] != 'free_warmup'
    status = current['status']
    if not full and status in {'converged', 'iteration_limit'}:
        status = 'evaluation_limit'
    return dict(status=status,
        positions_nm=np.array(current['positions_nm'], copy=True),
        energy_kj_mol=current['energy_kj_mol'], iterations=steps, evaluations=calls,
        projected_gradient_max_kj_mol_nm=(current['projected_gradient_max_kj_mol_nm'] if full else None),
        full_residual_available=full,
        accepted_energy_history_kj_mol=[e for p in phases for e in p['accepted_energy_history_kj_mol']],
        phases=phases, scientific_acceptance=False)


def numerical_receipt(result):
    """Serialize numerical evidence without coordinate arrays or admission claims."""
    return {k: ([{name: value for name, value in phase.items() if name != 'positions_nm'}
                 for phase in result['phases']] if k == 'phases' else v)
            for k, v in result.items() if k != 'positions_nm'}


def validate_block_receipt(receipt, policy):
    """Reject incomplete or inconsistent convergence claims; never admit a receptor."""
    import math
    from .receptor_bounded_minimizer import validate_optimizer_receipt
    common = {'status', 'energy_kj_mol', 'projected_gradient_max_kj_mol_nm',
              'iterations', 'evaluations', 'accepted_energy_history_kj_mol', 'scientific_acceptance'}
    if (not isinstance(receipt, dict)
            or set(receipt) != common | {'full_residual_available', 'phases'}
            or receipt['status'] != 'converged'
            or receipt['scientific_acceptance'] is not False
            or receipt['full_residual_available'] is not True):
        raise ValueError('Block receipt requires full-system numerical convergence')
    phases = receipt['phases']
    if not isinstance(phases, list) or len(phases) not in (1, 3):
        raise ValueError('Invalid block receipt phase sequence')
    expected = ['initial_full_residual'] if len(phases) == 1 else [
        'initial_full_residual', 'free_warmup', 'full_bounded']
    used_steps = used_calls = 0
    history = []
    for i, (phase, name) in enumerate(zip(phases, expected)):
        if not isinstance(phase, dict) or set(phase) != common | {'phase'} or phase['phase'] != name:
            raise ValueError('Invalid block receipt phase identity')
        cap = 0 if i == 0 else (policy['warmup_iterations'] if i == 1 else policy['max_iterations']-used_steps)
        steps, calls = phase['iterations'], phase['evaluations']
        energies, residual = phase['accepted_energy_history_kj_mol'], phase['projected_gradient_max_kj_mol_nm']
        if (type(steps) is not int or not 0 <= steps <= cap
                or type(calls) is not int or not steps+1 <= calls <= policy['max_evaluations']-used_calls
                or phase['scientific_acceptance'] is not False
                or type(residual) not in (int, float) or not math.isfinite(residual) or residual < 0
                or not isinstance(energies, list) or len(energies) != steps+1
                or any(type(e) not in (int, float) or not math.isfinite(e) for e in energies)
                or any(b > a for a,b in zip(energies, energies[1:]))
                or phase['energy_kj_mol'] != energies[-1]):
            raise ValueError('Invalid block phase counters, energy or residual')
        if i == 0 and calls != 1:
            raise ValueError('Initial full residual must consume exactly one evaluation')
        if i == 0 and len(phases) == 3:
            if phase['status'] != 'evaluation_limit' or residual <= policy['tolerance']:
                raise ValueError('Invalid initial phase transition')
        elif i == 1:
            if (phase['status'] not in {'converged', 'iteration_limit'}
                    or (phase['status'] == 'converged') != (residual <= policy['tolerance'])
                    or phase['status'] == 'iteration_limit' and steps != cap):
                raise ValueError('Invalid warmup phase transition')
        else:
            validate_optimizer_receipt({k:v for k,v in phase.items() if k != 'phase'},
                {**policy, 'max_iterations': max(1,cap),
                 'max_evaluations': policy['max_evaluations']-used_calls})
        # Phase starts evaluate the same unchanged coordinates. Require exact
        # continuity; a changed objective must not masquerade as one solve.
        if history and energies[0] != history[-1]:
            raise ValueError('Energy changed between block phases')
        used_steps += steps; used_calls += calls; history.extend(energies)
    final = phases[-1]
    if (type(receipt['iterations']) is not int or receipt['iterations'] != used_steps
            or type(receipt['evaluations']) is not int or receipt['evaluations'] != used_calls
            or receipt['accepted_energy_history_kj_mol'] != history
            or receipt['energy_kj_mol'] != final['energy_kj_mol']
            or receipt['projected_gradient_max_kj_mol_nm'] != final['projected_gradient_max_kj_mol_nm']):
        raise ValueError('Block totals differ from phase evidence')


def minimize_openmm_free_then_bounded(context, reference_nm, radii_nm, fixed_indices, **options):
    """Adapter sharing the original context validation and restoration."""
    from .receptor_bounded_minimizer import _minimize_openmm
    return _minimize_openmm(context, reference_nm, radii_nm, fixed_indices,
                           minimize_free_then_bounded, **options)
