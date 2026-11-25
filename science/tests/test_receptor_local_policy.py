"""Synthetic atom policies and force physics; no receptor repair computation."""
from copy import deepcopy
import pytest
from covscreen.receptor_local_policy import make_policy, bind_policy, audit_motion, position_force, validate_policy


def atoms():
    return [dict(index=i, entity_position=position, name=name, chain='A', author_residue=str(position + 695),
        resname=resname, original_serial=i + 1, xyz_angstrom=[float(i), 0., 0.])
        for i, (position, name, resname) in enumerate([(102, 'SG', 'CYS'), (12, 'CA', 'LEU'),
            (12, 'CD1', 'LEU'), (13, 'CA', 'ALA')])]


def policy():
    return make_policy('a' * 64, atoms(), [12], [102], 102)


def test_binding_survives_reordering_but_not_source_identity_or_coordinate_changes():
    rows = atoms()
    assert set(bind_policy(policy(), list(reversed(rows)), 'a' * 64)) == {1, 2}
    for field, value in [('chain', 'B'), ('original_serial', 99), ('xyz_angstrom', [0., 1., 2.])]:
        changed = deepcopy(rows)
        changed[1][field] = value
        with pytest.raises(ValueError, match='identity|coordinate'):
            bind_policy(policy(), changed, 'a' * 64)
    with pytest.raises(ValueError, match='another source'):
        bind_policy(policy(), rows, 'b' * 64)
    partial = policy()
    partial['local_observation_mask']['atoms'].pop()
    with pytest.raises(ValueError, match='every observed'):
        bind_policy(partial, rows, 'a' * 64)


@pytest.mark.parametrize('change', ['target', 'duplicates', 'nonfinite', 'forcefield', 'weaken', 'fixed'])
def test_reject_unsafe_or_ambiguous_policy_changes(change):
    selected = policy()
    mask = selected['local_observation_mask']
    if change == 'target':
        mask['movable_entity_positions'].append(102)
    elif change == 'duplicates':
        mask['atoms'].append(deepcopy(mask['atoms'][0]))
    elif change == 'nonfinite':
        mask['atoms'][0]['reference_angstrom'][0] = float('nan')
    elif change == 'forcefield':
        selected['forcefield'] = 'another.xml'
    elif change == 'weaken':
        mask['atoms'][0]['k_kj_mol_nm2'] = 1.0
    else:
        mask['fixed_observed_tolerance_angstrom'] = 0.1
    with pytest.raises(ValueError):
        validate_policy(selected)


def test_independent_audit_checks_fixed_target_and_per_atom_limits_without_alignment():
    original = atoms()
    good = deepcopy(original)
    good[1]['xyz_angstrom'][1] = 0.19
    good[2]['xyz_angstrom'][1] = 0.49
    report = audit_motion(policy(), original, good, 'a' * 64)
    assert report['status'] == 'local_motion_limits_passed'
    assert report['movable_observed_atom_count'] == report['fixed_observed_atom_count'] == 2
    assert report['receptor_prepared'] is False
    for index, distance in [(0, 1e-6), (1, 0.201), (2, 0.501), (3, 1e-6)]:
        changed = deepcopy(good)
        changed[index]['xyz_angstrom'][1] = distance
        report = audit_motion(policy(), original, changed, 'a' * 64)
        assert report['status'] == 'local_motion_limits_failed'
        assert report['violations'][0]['original_serial'] == index + 1


def test_pocket_sidechains_use_tight_limit():
    selected = make_policy('a' * 64, atoms(), [12], [12, 102], 102)
    assert all(a['k_kj_mol_nm2'] == 10000. and a['max_displacement_angstrom'] == 0.2
               for a in selected['local_observation_mask']['atoms'])


def test_local_force_uses_distinct_per_atom_stiffness_and_angstrom_conversion():
    import openmm as mm
    from openmm import unit
    selected = policy()
    bound = bind_policy(selected, atoms(), 'a' * 64)
    system = mm.System()
    for index in range(4):
        system.addParticle(12. if index in bound else 0.)
    system.addForce(position_force(bound))
    integrator = mm.VerletIntegrator(0.001)
    context = mm.Context(system, integrator, mm.Platform.getPlatformByName('Reference'))
    # References are x=0.1 and x=0.2 nm. Move both +0.1 nm in y.
    context.setPositions([[0., 0., 0.], [0.1, 0.1, 0.], [0.2, 0.1, 0.], [0.3, 0., 0.]])
    state = context.getState(getEnergy=True, getForces=True)
    assert state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole) == pytest.approx(55.)
    forces = state.getForces(asNumpy=True).value_in_unit(unit.kilojoule_per_mole / unit.nanometer)
    assert forces[1, 1] == pytest.approx(-1000.)
    assert forces[2, 1] == pytest.approx(-100.)
    mm.LocalEnergyMinimizer.minimize(context, 0.001, 100)
    points = context.getState(getPositions=True).getPositions(asNumpy=True).value_in_unit(unit.nanometer)
    assert points[0].tolist() == [0., 0., 0.]
    assert points[3].tolist() == [0.3, 0., 0.]
    assert abs(points[1, 1]) < 1e-6 and abs(points[2, 1]) < 1e-6
    del context, integrator


def test_block_policy_is_distinct_and_preserves_source_mask_and_budget():
    from covscreen.receptor_local_policy import (make_bounded_policy,make_block_policy,
        validate_policy_version,digest,BOUNDED_OPTIMIZER,BLOCK_OPTIMIZER)
    old=make_bounded_policy(policy());saved=deepcopy(old)
    new=make_block_policy(old)
    assert old==saved and new['local_observation_mask']==old['local_observation_mask']
    assert {k:v for k,v in new.items() if k!='bounded_optimizer'}=={k:v for k,v in old.items() if k!='bounded_optimizer'}
    assert new['bounded_optimizer']['max_iterations']==BOUNDED_OPTIMIZER['max_iterations']
    assert new['bounded_optimizer']['max_evaluations']==BOUNDED_OPTIMIZER['max_evaluations']
    assert digest(new)!=digest(old)
    validate_policy_version(new,'v2-local-block')
    with pytest.raises(ValueError):validate_policy_version(new,'v2-local-bounded')
    with pytest.raises(ValueError):validate_policy_version(old,'v2-local-block')
    with pytest.raises(ValueError):make_block_policy(new)
    new['bounded_optimizer']['warmup_iterations']+=1
    with pytest.raises(ValueError):validate_policy(new)
    assert BLOCK_OPTIMIZER['warmup_iterations']==100
