"""Require all real replicas before estimating sampling uncertainty and structural priority."""
import argparse
import csv
import json
from pathlib import Path
import sys
import numpy as np
from audit_openmm_pilot_20261002 import digest as sha
from replica_statistics_20261003 import replica_summary, drift_diagnostic

RULES={'version':'replica-sampling-diagnostics-v1','chosen_before_new_replica_results':True,
    'operational_drift_triggers':{'largest_ring_system_RMSD_A':{'absolute_trigger':.5},
        'initial_residue_contact_retention_no_C797':{'absolute_trigger':.15},
        'temperature_K':{'absolute_trigger':3.},'density_g_ml':{'relative_trigger':.02},
        'potential_kj_mol':{'relative_trigger':.005}},
    'between_replica_SD_triggers':{'largest_ring_system_RMSD_A':.5,'initial_residue_contact_retention_no_C797':.1},
    'tracked_HB_first_last_block_trigger':.2,
    'meaning':'Project-specific flags for additional sampling, not universal equilibrium criteria',
    'statistical_intervals':'Unadjusted descriptive Student t 95% intervals across three independent replica estimates. Normality cannot be assessed reliably at n=3.',
    'structural_frontier':'Lower ring RMSD and higher input-contact retention only describe input-pose conservation. Neither is binding affinity.',
    'no_experimental_activity_claim':True}


def summarize(root,rules):
    plan=json.loads((root/'execution-plan.json').read_text())
    assert len(plan['runs'])==12
    policy_hash=sha(root/'evaluation-policy.json')
    missing=[r['run_id'] for r in plan['runs'] if not (root/'runs'/r['run_id']/'analysis-v1/metrics-manifest.json').exists()]
    if missing:return {'status':'WAITING_FOR_REAL_REPLICAS','missing_run_ids':missing,'scientific_priority_available':False}
    records=[];velocities=set()
    for entry in plan['runs']:
        run=root/'runs'/entry['run_id'];p=run/'analysis-v1';m=json.loads((p/'metrics-manifest.json').read_text())
        assert m['status']=='REPLICA_METRICS_BOUND','CPU smoke outputs must never be counted as real replicas'
        assert m['candidate']==entry['candidate'] and m['replica']==entry['replica'] and m['frames']==1000
        assert m['policy_sha256']==policy_hash and m['input_manifest_sha256']==entry['input_manifest_sha256']==sha(run/'input-manifest.json')
        for file,digest in m['files'].items():assert sha(p/file)==digest,file
        a=json.loads((p/'audit.json').read_text());assert not a['CPU_smoke_only'] and a['production_ps']==10000
        assert a['DCD_sha256']==m['DCD_sha256'] and a['initial_velocity_file_sha256']==m['initial_velocity_sha256']
        assert m['initial_velocity_sha256'] not in velocities,'Copied initial velocities are not independent replicas'
        velocities.add(m['initial_velocity_sha256'])
        data=list(csv.DictReader((p/'trajectory-measurements.csv').open()))
        contacts=list(csv.DictReader((p/'contact-frames.csv').open()))
        assert len(data)==len(contacts)==1000
        for rows in [data,contacts]:assert np.allclose([float(r['production_time_ps']) for r in rows],np.arange(10,10001,10))
        series={k:np.array([float(r[k]) for r in data]) for k in data[0] if k!='production_time_ps'}
        series['initial_residue_contact_retention_no_C797']=np.array([float(r['initial_residue_contact_retention_no_C797']) for r in contacts])
        assert all(np.isfinite(v).all() for v in series.values())
        diagnostics={k:drift_diagnostic(series[k],**v) for k,v in rules['operational_drift_triggers'].items()}
        flags=[k for k,v in diagnostics.items() if v['operational_drift_flag']]
        hb=list(csv.DictReader((p/'hydrogen-bond-frames.csv').open()));hbsummary=[]
        for pair in sorted(set(r['pair'] for r in hb)):
            rows=[r for r in hb if r['pair']==pair];assert len(rows)==1000
            assert np.allclose([float(r['production_time_ps']) for r in rows],np.arange(10,10001,10))
            values=np.array([r['present_3_5A_150deg']=='True' for r in rows],dtype=float)
            d=drift_diagnostic(values,absolute_trigger=rules['tracked_HB_first_last_block_trigger'])
            if d['operational_drift_flag']:flags.append('HB:'+pair)
            hbsummary.append({'pair':pair,'protein_residue':int(rows[0]['protein_residue']),
                'occupancy_3_5A_150deg':float(values.mean()),'occupancy_3_0A_150deg':float(np.mean([r['present_3_0A_150deg']=='True' for r in rows])),
                'sampling_diagnostic':d})
        records.append({'candidate':entry['candidate'],'replica':entry['replica'],'run_id':entry['run_id'],
            'means':{k:float(v.mean()) for k,v in series.items()},'diagnostics':diagnostics,'HB_summary':hbsummary,
            'additional_sampling_flags':flags,'metrics_manifest_sha256':sha(p/'metrics-manifest.json'),
            'initial_velocity_sha256':m['initial_velocity_sha256']})
    systems={}
    for name in sorted(set(r['candidate'] for r in records)):
        runs=sorted([r for r in records if r['candidate']==name],key=lambda x:x['replica'])
        assert [r['replica'] for r in runs]==[1,2,3]
        estimates={key:replica_summary([r['means'][key] for r in runs]) for key in runs[0]['means']}
        flags=[f"replica_{r['replica']}:{flag}" for r in runs for flag in r['additional_sampling_flags']]
        for key,trigger in rules['between_replica_SD_triggers'].items():
            if estimates[key]['sd_between_replicas']>trigger:flags.append('between_replica_variation:'+key)
        pairs=sorted(set(h['pair'] for r in runs for h in r['HB_summary']))
        hbs={pair:{k:replica_summary([next(h[k] for h in r['HB_summary'] if h['pair']==pair) for r in runs])
            for k in ['occupancy_3_5A_150deg','occupancy_3_0A_150deg']} for pair in pairs}
        systems[name]={'sampling_assessment':'SAMPLING_INSUFFICIENT' if flags else 'NO_CLEAR_DRIFT_IN_OBSERVED_WINDOW',
            'additional_sampling_flags':flags,'replica_estimates':estimates,'HB_replica_estimates':hbs,
            'equilibrium_proven':False,'source_run_ids':[r['run_id'] for r in runs]}
    eligible=[n for n,s in systems.items() if n!='control_afatinib' and s['sampling_assessment']=='NO_CLEAR_DRIFT_IN_OBSERVED_WINDOW']
    comparisons=[];dominated=set()
    for a in eligible:
        for b in eligible:
            if a==b:continue
            first=systems[a]['replica_estimates'];second=systems[b]['replica_estimates']
            low=first['largest_ring_system_RMSD_A']['mean']<=second['largest_ring_system_RMSD_A']['mean']
            high=first['initial_residue_contact_retention_no_C797']['mean']>=second['initial_residue_contact_retention_no_C797']['mean']
            strict=first['largest_ring_system_RMSD_A']['mean']<second['largest_ring_system_RMSD_A']['mean'] or first['initial_residue_contact_retention_no_C797']['mean']>second['initial_residue_contact_retention_no_C797']['mean']
            if low and high and strict:
                dominated.add(b)
                interval_separated=(first['largest_ring_system_RMSD_A']['descriptive_t95_interval'][1]<second['largest_ring_system_RMSD_A']['descriptive_t95_interval'][0] and
                    first['initial_residue_contact_retention_no_C797']['descriptive_t95_interval'][0]>second['initial_residue_contact_retention_no_C797']['descriptive_t95_interval'][1])
                comparisons.append({'more_input_pose_conserving_by_means':a,'comparison_candidate':b,
                    'both_descriptive_intervals_separated':interval_separated,'formal_pairwise_significance_claimed':False})
    control_inadequate=systems['control_afatinib']['sampling_assessment']=='SAMPLING_INSUFFICIENT'
    for name,s in systems.items():
        if name=='control_afatinib':s['followup_priority']='REFERENCE_CONTROL_REVIEW' if control_inadequate else 'REFERENCE_CONTROL'
        elif s['sampling_assessment']=='SAMPLING_INSUFFICIENT':s['followup_priority']='ADDITIONAL_SAMPLING_REQUIRED'
        elif control_inadequate:s['followup_priority']='PROVISIONAL_ONLY_CONTROL_SAMPLING_INSUFFICIENT'
        elif name not in dominated:s['followup_priority']='PROVISIONAL_INPUT_POSE_CONSERVATION_FRONTIER'
        else:s['followup_priority']='PROVISIONAL_SECONDARY_STRUCTURAL_FOLLOWUP'
    return {'status':'REPEATED_MD_SAMPLING_ASSESSED','systems':systems,'replicas':records,'comparisons':comparisons,
        'operational_rules':rules,'scientific_priority_available':True,'priority_is_provisional':True,
        'global_affinity_rank':None,'all_replicates_and_uncertainty_shown':True,
        'limitations':['Three replicas cannot establish full equilibrium or characterize all microstates.',
            'Intervals are descriptive and unadjusted, with unverified normality at n=3.',
            'Different ligands have different own-pose references and initial contact sets.',
            'No MD result upgrades AF3-CovDock consensus or proves covalent chemical reactivity.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=summarize(args.root,RULES)
    result['summarizer_sha256']=sha(Path(__file__))
    result['statistical_helper_sha256']=sha(Path(__file__).with_name('replica_statistics_20261003.py'))
    result['execution_plan_sha256']=sha(args.root/'execution-plan.json')
    result['evaluation_policy_sha256']=sha(args.root/'evaluation-policy.json')
    with args.output.open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps({'status':result['status'],'scientific_priority_available':result['scientific_priority_available']}))
    return 0 if result['scientific_priority_available'] else 3


if __name__=='__main__':sys.exit(main())
