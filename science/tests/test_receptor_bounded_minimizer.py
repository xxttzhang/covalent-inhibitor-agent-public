"""Analytic tests only; no receptor structures or scientific jobs."""
import numpy as np
import pytest
from covscreen.receptor_bounded_minimizer import minimize_bounded


def test_boundary_optimum_fixed_and_free_atoms_at_every_evaluation():
    ref=np.array([[-5.,2.,-3.],[1.,-2.,3.],[0.,0.,0.]])
    target=ref+[[7.,8.,9.],[1.,2.,2.],[3.,-1.,4.]]
    seen=[]
    def quadratic(x):
        assert np.array_equal(x[0],ref[0])
        assert np.linalg.norm(x[1]-ref[1]) <= .02
        seen.append(x.copy())
        return .5*np.sum((x-target)**2),x-target
    out=minimize_bounded(quadratic,ref,ref,{1:.02},[0],tolerance=1e-8,initial_step=.1)
    expected=ref.copy();expected[1]+=np.array([1.,2.,2.])*(.02/3);expected[2]=target[2]
    assert out['status']=='converged'
    assert np.allclose(out['positions_nm'],expected,atol=1e-9,rtol=0)
    assert len(seen)==out['evaluations']
    assert np.all(np.diff(out['accepted_energy_history_kj_mol']) <= 0)
    assert not out['scientific_acceptance']
    assert np.linalg.norm(out['positions_nm'][1]-target[1])>2


def test_sphere_is_rotation_invariant_not_cartesian_box():
    ref=np.zeros((1,3))
    for direction in [[1.,0.,0.],[1.,1.,1.],[-1.,2.,3.]]:
        target=np.array([direction])
        out=minimize_bounded(lambda x:(.5*np.sum((x-target)**2),x-target),ref,ref,{0:.05},[],tolerance=1e-8)
        assert out['status']=='converged'
        assert np.allclose(out['positions_nm'],target*.05/np.linalg.norm(target),atol=1e-10)


def test_stiff_coupled_energy_backtracks_and_preserves_domain():
    ref=np.zeros((2,3));matrix=np.array([[1000.,900.],[900.,1000.]])
    target=np.array([[.5,.5,.5],[-.5,-.5,-.5]])
    def coupled(x):
        assert np.linalg.norm(x[0])<=.02
        d=x-target
        return .5*np.sum(d*(matrix@d)),matrix@d
    out=minimize_bounded(coupled,ref,ref,{0:.02},[],initial_step=1.,tolerance=1e-5,max_iterations=2000)
    assert out['status']=='converged'
    assert out['evaluations']>out['iterations']+1
    assert np.linalg.norm(out['positions_nm'][0])==pytest.approx(.02)
    assert np.allclose(out['positions_nm'][1],target[1]-.9*(out['positions_nm'][0]-target[0]),atol=1e-7)


@pytest.mark.parametrize('kwargs,status',[({'max_iterations':1},'iteration_limit'),({'max_evaluations':1},'evaluation_limit'),({'max_backtracks':1,'initial_step':1.},'line_search_failed')])
def test_budget_exhaustion_does_not_claim_convergence(kwargs,status):
    ref=np.zeros((1,3))
    out=minimize_bounded(lambda x:(500*np.sum((x-1)**2),1000*(x-1)),ref,ref,{},[],tolerance=1e-10,**kwargs)
    assert out['status']==status
    assert not out['scientific_acceptance']
    assert out['evaluations']<=kwargs.get('max_evaluations',10000)


@pytest.mark.parametrize('change',['outside','fixed','overlap','duplicate','negative_radius','nan'])
def test_invalid_domain_rejected_before_evaluation(change):
    ref=np.zeros((2,3));initial=ref.copy();radii={1:.02};fixed=[0]
    if change=='outside':initial[1,0]=.020001
    elif change=='fixed':initial[0,0]=1e-15
    elif change=='overlap':radii[0]=.1
    elif change=='duplicate':fixed=[0,0]
    elif change=='negative_radius':radii[1]=-.1
    elif change=='nan':ref[0,0]=float('nan')
    def forbidden(_):pytest.fail('Must not evaluate invalid domain')
    with pytest.raises(ValueError):minimize_bounded(forbidden,initial,ref,radii,fixed)


@pytest.mark.parametrize('bad',['energy','gradient','shape'])
def test_nonfinite_or_invalid_oracle_rejected(bad):
    def oracle(x):
        if bad=='energy':return float('nan'),x
        if bad=='gradient':return 0.,x+float('inf')
        return 0.,np.zeros(3)
    with pytest.raises(ValueError):minimize_bounded(oracle,np.zeros((1,3)),np.zeros((1,3)),{},[])


def test_openmm_force_sign_units_fixed_atom_and_context_state():
    import openmm as mm
    from openmm import unit
    from covscreen.receptor_bounded_minimizer import minimize_openmm_bounded
    system=mm.System()
    for mass in [0.,12.,12.]:system.addParticle(mass)
    force=mm.CustomExternalForce('0.5*k*((x-tx)^2+y*y+z*z)')
    force.addPerParticleParameter('k');force.addPerParticleParameter('tx')
    for i,target in enumerate([1.,1.,.5]):force.addParticle(i,[1000.,target])
    system.addForce(force)
    integrator=mm.VerletIntegrator(.001)
    context=mm.Context(system,integrator,mm.Platform.getPlatformByName('Reference'))
    context.setPositions(np.zeros((3,3))*unit.nanometer)
    out=minimize_openmm_bounded(context,np.zeros((3,3)),{1:.02},[0],tolerance=1e-6)
    assert out['status']=='converged'
    assert np.allclose(out['positions_nm'],[[0.,0.,0.],[.02,0.,0.],[.5,0.,0.]],atol=1e-8)
    actual=context.getState(getPositions=True,getEnergy=True)
    assert np.array_equal(actual.getPositions(asNumpy=True).value_in_unit(unit.nanometer),out['positions_nm'])
    assert actual.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)==pytest.approx(980.2)
    with pytest.raises(ValueError,match='zero-mass'):
        minimize_openmm_bounded(context,np.zeros((3,3)),{1:.02},[])
    del context,integrator


def test_oracle_mutation_does_not_change_accepted_state():
    ref=np.zeros((1,3))
    def oracle(x):
        gradient=x-1;energy=.5*np.sum(gradient**2)
        x[:]=100
        return energy,gradient
    out=minimize_bounded(oracle,ref,ref,{0:.02},[],tolerance=1e-8)
    assert out['status']=='converged'
    assert np.linalg.norm(out['positions_nm'])<=.02


def test_translated_small_sphere_remains_feasible():
    ref=np.array([[5.123456789,-2.13456789,3.3456789]])
    target=ref+[[1.,2.,3.]]
    out=minimize_bounded(lambda x:(.5*np.sum((x-target)**2),x-target),ref,ref,{0:.02},[],tolerance=1e-8)
    assert out['status']=='converged'
    assert np.linalg.norm(out['positions_nm']-ref)<=.02


def test_translated_ball_roundoff_never_evaluates_outside_radius():
    rng=np.random.default_rng(129)
    for _ in range(200):
        ref=rng.uniform(-10,10,(1,3));target=ref+rng.normal(size=(1,3))
        radius=float(rng.choice([.02,.05]))
        def oracle(x):
            assert np.linalg.norm(x-ref)<=radius
            return .5*np.sum((x-target)**2),x-target
        out=minimize_bounded(oracle,ref,ref,{0:radius},[],tolerance=1e-8)
        assert out['status']=='converged'
        assert np.linalg.norm(out['positions_nm']-ref)<=radius
        expected=ref+(target-ref)*radius/np.linalg.norm(target-ref)
        assert np.allclose(out['positions_nm'],expected,atol=1e-9,rtol=0)


def test_nm_to_angstrom_output_survives_independent_motion_audit():
    from copy import deepcopy
    from covscreen.receptor_local_policy import make_policy,bind_policy,audit_motion
    original=[dict(index=i,entity_position=p,name=n,chain='A',author_residue=str(p+695),
                   resname=r,original_serial=i+1,xyz_angstrom=xyz)
              for i,(p,n,r,xyz) in enumerate([(102,'SG','CYS',[-25.,14.,30.]),
                  (12,'CA','LEU',[-23.1367256234,-53.2192243176,79.6255651729]),
                  (12,'CD1','LEU',[-24.,-52.,80.])])]
    policy=make_policy('a'*64,original,[12],[102],102)
    selected=bind_policy(policy,original,'a'*64)
    reference=np.array([a['xyz_angstrom'] for a in original])/10
    radii={i:a['max_displacement_angstrom']/10 for i,a in selected.items()}
    target=reference+[[1.,2.,3.],[-1.,2.,-3.],[2.,-3.,1.]]
    out=minimize_bounded(lambda x:(.5*np.sum((x-target)**2),x-target),reference,reference,radii,[0],tolerance=1e-8)
    assert out['status']=='converged'
    final=deepcopy(original)
    for atom,point in zip(final,out['positions_nm']*10):atom['xyz_angstrom']=point.tolist()
    audit=audit_motion(policy,original,final,'a'*64)
    assert audit['status']=='local_motion_limits_passed'
    assert not audit['receptor_prepared']


def test_distinct_bounded_policy_preserves_old_policy_and_limits():
    from copy import deepcopy
    from covscreen.receptor_local_policy import make_bounded_policy,validate_policy_version
    from test_receptor_local_policy import policy
    old=policy();before=deepcopy(old);bounded=make_bounded_policy(old)
    assert old==before
    assert {k:v for k,v in bounded.items() if k!='bounded_optimizer'}==old
    validate_policy_version(old,'v2-local');validate_policy_version(bounded,'v2-local-bounded')
    for p,v in [(bounded,'v2-local'),(old,'v2-local-bounded')]:
        with pytest.raises(ValueError,match='version'):validate_policy_version(p,v)
    bounded['bounded_optimizer']['max_evaluations']+=1
    with pytest.raises(ValueError,match='configuration'):validate_policy_version(bounded,'v2-local-bounded')


@pytest.mark.parametrize('tamper',['status','residual','evaluations','history','acceptance'])
def test_receipt_cannot_claim_convergence_or_ignore_budget(tamper):
    from covscreen.receptor_bounded_minimizer import validate_optimizer_receipt
    from covscreen.receptor_local_policy import BOUNDED_OPTIMIZER
    ref=np.zeros((1,3));out=minimize_bounded(lambda x:(.5*np.sum(x*x),x),ref,ref,{},[])
    out.pop('positions_nm');validate_optimizer_receipt(out,BOUNDED_OPTIMIZER)
    if tamper=='status':out['status']='iteration_limit'
    elif tamper=='residual':out['projected_gradient_max_kj_mol_nm']=11.
    elif tamper=='evaluations':out['evaluations']=3001
    elif tamper=='history':out['accepted_energy_history_kj_mol']=[1.,0.]
    else:out['scientific_acceptance']=True
    with pytest.raises(ValueError):validate_optimizer_receipt(out,BOUNDED_OPTIMIZER)
