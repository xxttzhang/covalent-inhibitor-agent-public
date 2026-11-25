"""Artificial study reviews exercise portable delivery and existing pose readers."""
import copy
import hashlib
import json
import shutil

import pandas as pd
import pytest

from covscreen.jobs import sha256_file as sha
from covscreen.md_review_delivery import PROFILE, freeze
from covscreen.md_candidate_delivery import load_shared_candidate_delivery
from covscreen.md_candidate_pose import freeze_delivered_candidate_pose, load_candidate_pose_input
from covscreen.refinement_input import verify_refinement_input


@pytest.fixture
def case(tmp_path):
    base=tmp_path/'source';base.mkdir()
    table=base/'input.csv'
    pd.DataFrame([{'compound_id':name,'input_sdf_index':i,'standardized_smiles':'C=CC(=O)NC',
        'target_id':'EGFR','site_id':'EGFR_C797','parent_id':'0002','warhead_class':'acrylamide',
        'electrophilic_atom_map':101,'route':'ligand-first'} for i,name in [(7,'007'),(9,'failed')]]).to_csv(table,index=False)
    digest=hashlib.sha256(json.dumps([[7,'007'],[9,'failed']],separators=(',',':')).encode()).hexdigest()
    contract=verify_refinement_input(table,2,sha(table),digest)
    identity={'entry_id':'1M17','uniprot':'P00533','canonical_cys':797,'author_chain':'A','author_residue':'773'}
    prepared=base/'prepared.json';prepared.write_text(json.dumps({'schema':'prepared-receptor-artifact-v1',
        'status':'prepared_isolated_protein_scope',**identity,'receptor_prepared':True,'campaign_ready':False}))
    context=base/'context.json';context.write_text(json.dumps({'schema':'receptor-site-context-artifact-v1',
        'status':'descriptive_site_context_completed_review_pending','identity':identity,
        'parents':{'prepared_receptor_sha256':sha(prepared)},'campaign_ready':False}))
    (base/'poses.maegz').write_bytes(b'ARTIFICIAL MAESTRO PLACEHOLDER')
    (base/'model.cif').write_bytes(b'ARTIFICIAL CIF PLACEHOLDER')
    gate={'physical_validity':'PASS','cross_method_consensus':'NA','covdock_gate':'B','covdock_gate_detail':'B2','review_eligible':False}
    candidates=[{'compound_id':'007','input_sdf_index':7,'engine_status':'SUCCESS','score':-7.,'failure_reason':None,
                 'structure':{'path':'poses.maegz','pose_index':0},'gate':gate},
                {'compound_id':'failed','input_sdf_index':9,'engine_status':'FAILED','score':None,
                 'failure_reason':'artificial_failure','structure':None,'gate':{**gate,'physical_validity':'UNKNOWN'}}]
    covdock={'schema':'ligand-covdock-measurement-review-v1','run_id':'covdock-test-001','input_contract':contract,
        'candidate_artifact_id':'fixture-candidates','candidate_count':2,'candidates':candidates,
        'attachment_residue':'A:773','output_sha256':{'poses.maegz':sha(base/'poses.maegz')}}
    af3=copy.deepcopy(covdock);af3.update(schema='ligand-af3-measurement-review-v1',run_id='af3-test-001',
        output_sha256={'model.cif':sha(base/'model.cif')},target_artifact_id='fixture-target',
        target_mapping={'source':{'entry_id':'1M17','auth_asym_id':'A','author_residue_id':'773','uniprot_residue':797}})
    af3['candidates'][0].update(score=7.5,structure={'path':'model.cif','pose_index':None})
    source={'schema':PROFILE,'route':'ligand-first','workflow_id':'fixture-flow','task_id':'covdock-test-001',
        'candidate_artifact_id':'fixture-candidates','reaction_type':'Michael Addition','receptor_sha256':'a'*64,
        'target_artifact_id':'fixture-target',
        'covdock':covdock,'af3':af3}
    return source,table,prepared,context,lambda name: base/name.split('/',1)[1]


def deliver(tmp_path,case):
    source,table,prepared,context,resolve=case
    output=tmp_path/'delivery'
    value=freeze(source,table,prepared,context,output,resolve)
    path=output/'candidate-results.json'
    return value,path


def test_current_review_delivery_selects_pose_and_replays_after_source_removal(tmp_path,case):
    value,path=deliver(tmp_path,case)
    assert value['candidate_count']==2
    assert [r['compound_id'] for r in value['candidates']]==['007','failed']
    assert value['candidates'][0]['parent_id']=='0002'
    assert value['candidates'][0]['electrophilic_atom']=='map:101'
    assert value['candidates'][0]['nucleophilic_atom']=='A:CYS:773:SG'
    assert value['pose_selection_assessment'][0]['state']=='eligible_for_pose_selection'
    assert value['pose_selection_assessment'][0]['input_sdf_index']==7
    assert type(value['pose_selection_assessment'][0]['input_sdf_index']) is int
    assert value['pose_selection_assessment'][1]['state']=='ineligible_for_pose_selection'
    assert value['md_admission']['state']=='not_evaluated' and value['md_admission']['rejection_reasons']==[]
    assert not value['calculation_submitted']
    folder=tmp_path/'state/candidate_pose_input'/('b'*32)
    pose=freeze_delivered_candidate_pose(path,sha(path),'007',7,case[2],sha(case[2]),case[3],sha(case[3]),folder,
        expected_route='ligand-first',expected_task_id=case[0]['task_id'])
    assert pose['origin']['task_id']=='covdock-test-001' and not pose['native_graph_qc_completed']
    for base in ('source','delivery'):shutil.rmtree(tmp_path/base)
    loaded=load_candidate_pose_input(tmp_path,'candidate-pose-input-'+'b'*32,sha(folder/'candidate-pose-input.json'))
    assert loaded['manifest']==pose


def test_no_af3_is_preserved_as_unrun_not_invented_success(tmp_path,case):
    case[0]['af3']=None
    value,path=deliver(tmp_path,case)
    assert all(r['af3_status']=='NOT_RUN' for r in value['candidates'])
    assert all(r['state']=='ineligible_for_pose_selection' for r in value['pose_selection_assessment'])
    with pytest.raises(ValueError,match='structural evidence'):
        freeze_delivered_candidate_pose(path,sha(path),'007',7,case[2],sha(case[2]),case[3],sha(case[3]),tmp_path/'pose',
            expected_route='ligand-first',expected_task_id=case[0]['task_id'])


def test_same_target_identity_cannot_replace_reviewed_receptor_bytes(tmp_path,case):
    _,path=deliver(tmp_path,case)
    receptor=json.loads(case[2].read_text())
    receptor['unreviewed_change']='same identity, different prepared artifact'
    case[2].write_text(json.dumps(receptor))
    context=json.loads(case[3].read_text())
    context['parents']['prepared_receptor_sha256']=sha(case[2])
    case[3].write_text(json.dumps(context))
    with pytest.raises(ValueError,match='reviewed receptor parents'):
        freeze_delivered_candidate_pose(path,sha(path),'007',7,case[2],sha(case[2]),case[3],sha(case[3]),
            tmp_path/'pose',expected_route='ligand-first',expected_task_id=case[0]['task_id'])


@pytest.mark.parametrize('change',['identity','census','source_hash','reaction','target','af3_target','path'])
def test_projection_rejects_conflicting_reviews_before_freezing(tmp_path,case,change):
    source=case[0]
    if change=='identity':source['af3']['candidates'][0]['compound_id']='7'
    if change=='census':source['covdock']['candidates'].pop()
    if change=='source_hash':source['covdock']['output_sha256']['poses.maegz']='b'*64
    if change=='reaction':source['reaction_type']='other reaction'
    if change=='target':source['covdock']['attachment_residue']='A:797'
    if change=='af3_target':source['af3']['target_mapping']['source']['author_residue_id']='797'
    if change=='path':source['covdock']['candidates'][0]['structure']['path']='../poses.maegz'
    with pytest.raises(ValueError):deliver(tmp_path,case)


@pytest.mark.parametrize('change',['candidate','md_state','projection','asset','parent'])
def test_portable_replay_rejects_forged_derived_records(tmp_path,case,change):
    value,path=deliver(tmp_path,case)
    if change=='candidate':value['candidates'][0]['compound_id']='7'
    if change=='md_state':value['md_admission']['state']='rejected'
    if change=='projection':(path.parent/'projection.csv').write_text('forged')
    if change=='asset':(path.parent/next(iter(value['assets'].values()))['filename']).write_bytes(b'changed')
    if change=='parent':value['parents']['prepared_receptor']='b'*64
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):load_shared_candidate_delivery(path,sha(path),expected_route='ligand-first',expected_task_id='covdock-test-001')
