"""Analytic objectives only; no molecular execution."""
import numpy as np
import pytest
from covscreen.receptor_block_minimizer import minimize_free_then_bounded


def test_coupled_solution_requires_joint_optimization():
    ref=np.zeros((3,3));matrix=np.array([[100.,30.],[30.,20.]])
    target=np.array([[.5,.4,.2],[-.5,-.3,.1]]);seen=[]
    def objective(x):
        assert np.array_equal(x[0],ref[0])
        assert np.linalg.norm(x[1])<=.02
        seen.append(x.copy());d=x[1:]-target;g=np.zeros_like(x);g[1:]=matrix@d
        return .5*np.sum(d*(matrix@d)),g
    out=minimize_free_then_bounded(objective,ref,ref,{1:.02},[0],
        warmup_iterations=20,max_iterations=2000,max_evaluations=5000,tolerance=1e-5)
    assert out['status']=='converged' and out['full_residual_available']
    expected=target[0]*.02/np.linalg.norm(target[0])
    assert np.allclose(out['positions_nm'][1],expected,atol=1e-7)
    assert np.allclose(out['positions_nm'][2],target[1]-1.5*(expected-target[0]),atol=1e-6)
    assert len(seen)==out['evaluations']<=5000
    assert out['iterations']<=2000 and not out['scientific_acceptance']
    assert np.all(np.diff(out['accepted_energy_history_kj_mol'])<=1e-12)
    assert np.array_equal(out['phases'][1]['positions_nm'][1],ref[1])


@pytest.mark.parametrize('budget',[1,2,3,10])
def test_shared_budget_and_no_false_full_convergence(budget):
    ref=np.zeros((2,3));calls=[]
    def objective(x):
        calls.append(x.copy());return .5*np.sum((x-1)**2),x-1
    out=minimize_free_then_bounded(objective,ref,ref,{0:.02},[],
        warmup_iterations=3,max_iterations=10,max_evaluations=budget,tolerance=1e-9)
    assert len(calls)==out['evaluations']<=budget
    if out['phases'][-1]['phase']=='free_warmup':
        assert out['status']!='converged'
        assert out['projected_gradient_max_kj_mol_nm'] is None
        assert not out['full_residual_available']
    assert not out['scientific_acceptance']


def test_stationary_free_block_cannot_hide_observed_gradient():
    ref=np.zeros((2,3))
    def objective(x):
        d=x.copy();d[0]-=1;return .5*np.sum(d*d),d
    out=minimize_free_then_bounded(objective,ref,ref,{0:.02},[],
        warmup_iterations=3,max_iterations=10,max_evaluations=2,tolerance=1e-9)
    assert out['phases'][-1]['status']=='converged'
    assert out['status']=='evaluation_limit' and not out['full_residual_available']


def test_invalid_initial_domain_rejected_before_oracle():
    ref=np.zeros((2,3));initial=ref.copy();initial[0,0]=.03
    def forbidden(x):raise AssertionError('invalid coordinates')
    with pytest.raises(ValueError,match='violate'):
        minimize_free_then_bounded(forbidden,initial,ref,{0:.02},[])


def test_failed_free_line_search_stops_without_joint_fallback():
    ref=np.zeros((2,3));calls=[]
    def inconsistent_oracle(x):
        calls.append(x.copy())
        return float(np.sum(x*x)),np.ones_like(x)
    out=minimize_free_then_bounded(inconsistent_oracle,ref,ref,{0:.02},[],
        max_backtracks=1,max_evaluations=30)
    assert out['status']=='line_search_failed'
    assert [p['phase'] for p in out['phases']]==['initial_full_residual','free_warmup']
    assert len(calls)==out['evaluations']==3
    assert not out['full_residual_available']
    assert np.array_equal(out['positions_nm'],ref)


def test_nonfinite_warmup_oracle_stops_without_fallback():
    calls=[];ref=np.zeros((2,3))
    def oracle(x):
        calls.append(x.copy())
        return (float('nan') if len(calls)==3 else .5*np.sum((x-1)**2)),x-1
    with pytest.raises(ValueError,match='not finite'):
        minimize_free_then_bounded(oracle,ref,ref,{0:.02},[])
    assert len(calls)==3


@pytest.mark.parametrize('seed',range(6))
def test_rotated_stiff_coupling_has_independent_full_residual(seed):
    rng=np.random.default_rng(seed)
    q,_=np.linalg.qr(rng.normal(size=(6,6)))
    h=q@np.diag([1.,3.,10.,30.,100.,300.])@q.T
    ref=np.zeros((3,3));target=rng.normal(size=6)*.1
    def oracle(x):
        assert np.array_equal(x[0],ref[0])
        assert np.linalg.norm(x[1])<=.02
        delta=x[1:].reshape(-1)-target;g=np.zeros_like(x)
        g[1:]=(h@delta).reshape(2,3)
        return .5*delta@(h@delta),g
    out=minimize_free_then_bounded(oracle,ref,ref,{1:.02},[0],
        warmup_iterations=30,max_iterations=2000,max_evaluations=5000,tolerance=1e-5)
    x=out['positions_nm'];_,g=oracle(x);y=x-.001*g;y[0]=ref[0]
    if np.linalg.norm(y[1])>.02:y[1]*=.02/np.linalg.norm(y[1])
    independent=np.max(np.linalg.norm((x-y)/.001,axis=1))
    assert out['status']=='converged' and independent<=1.00001e-5
    assert out['projected_gradient_max_kj_mol_nm']==pytest.approx(independent,abs=1e-9)
    assert np.all(np.diff(out['accepted_energy_history_kj_mol'])<=1e-12)
    assert not out['scientific_acceptance']


def converged_receipt():
    from covscreen.receptor_block_minimizer import numerical_receipt
    ref=np.zeros((2,3))
    policy=dict(warmup_iterations=3,max_iterations=30,max_evaluations=100,tolerance=1e-6)
    result=minimize_free_then_bounded(lambda x:(.5*np.sum((x-1)**2),x-1),ref,ref,{0:.02},[],**policy)
    return numerical_receipt(result),policy


def test_block_receipt_roundtrip_is_coordinate_free_and_not_admission():
    import json
    from covscreen.receptor_block_minimizer import validate_block_receipt
    receipt,policy=converged_receipt()
    receipt=json.loads(json.dumps(receipt,allow_nan=False))
    validate_block_receipt(receipt,policy)
    assert not receipt['scientific_acceptance']
    assert all('positions_nm' not in p for p in receipt['phases'])


@pytest.mark.parametrize('change',['order','budget','total','residual','free_only','warm_failure','energy','acceptance','missing'])
def test_block_receipt_rejects_forged_convergence(change):
    from covscreen.receptor_block_minimizer import validate_block_receipt
    receipt,policy=converged_receipt()
    if change=='order':receipt['phases'][1]['phase']='full_bounded'
    elif change=='budget':policy['max_evaluations']=1
    elif change=='total':receipt['evaluations']+=1
    elif change=='residual':receipt['projected_gradient_max_kj_mol_nm']=999.
    elif change=='free_only':receipt['phases']=receipt['phases'][:2]
    elif change=='warm_failure':receipt['phases'][1]['status']='line_search_failed'
    elif change=='energy':receipt['phases'][1]['accepted_energy_history_kj_mol'][0]+=1
    elif change=='acceptance':receipt['scientific_acceptance']=True
    else:del receipt['full_residual_available']
    with pytest.raises(ValueError):validate_block_receipt(receipt,policy)


def test_already_stationary_full_system_receipt():
    from covscreen.receptor_block_minimizer import numerical_receipt,validate_block_receipt
    ref=np.zeros((2,3));policy=dict(warmup_iterations=3,max_iterations=30,max_evaluations=100,tolerance=1e-6)
    result=minimize_free_then_bounded(lambda x:(.5*np.sum(x*x),x),ref,ref,{0:.02},[],**policy)
    assert result['evaluations']==1
    validate_block_receipt(numerical_receipt(result),policy)


def test_openmm_block_adapter_retains_feasible_result_and_restores_on_error():
    mm=pytest.importorskip('openmm')
    from openmm import unit
    from covscreen.receptor_block_minimizer import minimize_openmm_free_then_bounded
    system=mm.System()
    for mass in [0.,12.,12.]:system.addParticle(mass)
    force=mm.CustomExternalForce('0.5*k*((x-0.1)^2+y*y+z*z)')
    force.addGlobalParameter('k',100.)
    for i in range(3):force.addParticle(i,[])
    system.addForce(force);integrator=mm.VerletIntegrator(.001)
    context=mm.Context(system,integrator,mm.Platform.getPlatformByName('Reference'))
    ref=np.zeros((3,3));context.setPositions(ref*unit.nanometer)
    result=minimize_openmm_free_then_bounded(context,ref,{1:.02},[0],
        warmup_iterations=5,max_iterations=100,max_evaluations=300,tolerance=1e-6)
    current=np.asarray(context.getState(getPositions=True).getPositions(asNumpy=True).value_in_unit(unit.nanometer))
    assert result['status']=='converged'
    assert np.array_equal(current,result['positions_nm'])
    assert np.array_equal(current[0],ref[0]) and np.linalg.norm(current[1])<=.02
    assert current[2,0]==pytest.approx(.1,abs=1e-8)
    # Force an invalid energy after the successful state has been installed.
    context.setParameter('k',float('nan'))
    before=current.copy()
    with pytest.raises(ValueError,match='not finite'):
        minimize_openmm_free_then_bounded(context,ref,{1:.02},[0])
    after=np.asarray(context.getState(getPositions=True).getPositions(asNumpy=True).value_in_unit(unit.nanometer))
    assert np.array_equal(after,before)
    del context,integrator
