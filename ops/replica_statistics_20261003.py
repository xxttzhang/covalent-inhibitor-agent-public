"""Descriptive replica-level uncertainty and sampling diagnostics, not affinity tests."""
import numpy as np


def autocorrelation_diagnostic(values):
    x=np.asarray(values,dtype=float)
    if len(x)<20:return {'effective_frames_if_stationary':None,'reason':'fewer_than_20_frames'}
    x=x-x.mean();variance=float(np.mean(x*x))
    if variance<1e-20:return {'effective_frames_if_stationary':None,'reason':'constant_observable_no_sampling_information'}
    n=len(x);spectrum=np.fft.rfft(x,n=2*n)
    covariance=np.fft.irfft(spectrum*np.conjugate(spectrum),n=2*n)[:n]/n
    rho=covariance/covariance[0]
    # Positive-pair truncation; reported only as a stationarity-conditional diagnostic.
    summed=0.;last=0
    for lag in range(1,n-1,2):
        pair=float(rho[lag]+rho[lag+1])
        if pair<=0:break
        summed+=pair;last=lag+1
    inefficiency=max(1.,1+2*summed)
    return {'effective_frames_if_stationary':float(min(n,n/inefficiency)),
        'statistical_inefficiency':inefficiency,'positive_pair_last_lag':last,
        'method':'Biased FFT autocorrelation, positive adjacent lag-pair truncation; stationarity is not established by this calculation'}


def replica_summary(values):
    x=np.asarray(values,dtype=float)
    if x.shape!=(3,) or not np.isfinite(x).all():raise ValueError('Exactly three finite independent replica estimates required')
    mean=float(x.mean());sd=float(x.std(ddof=1));half=4.302652729749462*sd/np.sqrt(3)
    return {'replica_estimates':x.tolist(),'n_independent_replicas':3,'mean':mean,'sd_between_replicas':sd,
        'range':[float(x.min()),float(x.max())],'descriptive_t95_interval':[mean-half,mean+half],
        'interval_assumptions':'Independent, approximately normal replica means. n=3 cannot validate normality; not an equilibrium guarantee or hypothesis test.'}


def drift_diagnostic(values, *, absolute_trigger=None, relative_trigger=None):
    x=np.asarray(values,dtype=float)
    if len(x)!=1000 or not np.isfinite(x).all():raise ValueError('Expected all 1000 finite samples of a 10 ns replica')
    means=np.array([b.mean() for b in np.array_split(x,5)])
    span=float(means[-1]-means[0])
    trigger=absolute_trigger if absolute_trigger is not None else relative_trigger*max(abs(float(x.mean())),1e-12)
    monotonic=bool(np.all(np.diff(means)>=0) or np.all(np.diff(means)<=0))
    return {'two_ns_block_means':means.tolist(),'last_minus_first_block':span,'all_blocks_monotonic':monotonic,
        'operational_drift_flag':bool(abs(span)>trigger),'operational_trigger':float(trigger),
        'interpretation':'Prespecified screening flag, not a universal convergence threshold',
        **autocorrelation_diagnostic(x)}


def self_test():
    rng=np.random.default_rng(20261003)
    white=rng.normal(size=20000)
    correlated=np.empty_like(white);correlated[0]=white[0]
    for i in range(1,len(white)):correlated[i]=.95*correlated[i-1]+white[i]
    a=autocorrelation_diagnostic(white);b=autocorrelation_diagnostic(correlated)
    assert a['effective_frames_if_stationary']>10000 and b['effective_frames_if_stationary']<2000
    assert autocorrelation_diagnostic(np.ones(1000))['effective_frames_if_stationary'] is None
    assert drift_diagnostic(np.linspace(0,2,1000),absolute_trigger=.5)['operational_drift_flag']
    assert not drift_diagnostic(np.tile([-1.,1.],500),absolute_trigger=.5)['operational_drift_flag']
    summary=replica_summary([1,2,3]);assert np.isclose(summary['sd_between_replicas'],1)
    assert summary['descriptive_t95_interval'][0]<0 and summary['descriptive_t95_interval'][1]>4
    try:replica_summary([1,2])
    except ValueError:pass
    else:raise AssertionError('Missing replica was not rejected')
    return {'status':'PASSED','kind':'Synthetic numerical tests, not molecular dynamics results',
        'iid_effective_frames':a['effective_frames_if_stationary'],'AR1_effective_frames':b['effective_frames_if_stationary']}


if __name__=='__main__':
    import json
    print(json.dumps(self_test()))
