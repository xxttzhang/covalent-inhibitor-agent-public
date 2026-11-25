"""Synthetic Library provenance through the real public workflow, without compute."""
import csv
import json
from pathlib import Path

import pytest

from covscreen.md_historical_library_source import FILES, freeze
from covscreen.md_historical_source import load_source, sha
from covscreen.md_candidate_pose import load_candidate_pose_input
from covscreen.mcp.server import dispatch
from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.workflow_store import load
from test_md_historical_source import sources, write
from test_target_acquisition_handoff import handoff
from test_library_workflow import call, TARGET_ID, TARGET_SUFFIX, state_file


@pytest.fixture
def library(tmp_path, sources, handoff):
    paths = {k: sources[k] if k in sources else tmp_path/name for k, name in FILES.items()}
    compound = 'fixture-017'
    task, refinement, workflow = 'screen-'+'1'*24, 'refine-'+'2'*24, 'refineflow-'+'3'*24
    remote = '/historical/tasks/'+task+'/refinements/'+refinement+'/covdock/'
    origins = {k:str(p) for k, p in paths.items()}
    origins.update(pose_file=remote+'pose.maegz', candidate_table=remote+'core_subset_identity.csv')
    canonical = ''.join((tmp_path/'state/target_identity'/TARGET_SUFFIX/'P00533.fasta').read_text().splitlines()[1:])
    paths['construct_fasta'].write_text('>synthetic construct\n'+canonical[695:1016]+'\n')
    original = {'system_id':'library_017','compound_id':compound,'input_sdf_index':17, 'pose':32,
        'path':origins['pose_file'],'sha256':sha(paths['pose_file']), 'protein_chain':'A','target_residue':797,
        'reactant_smiles_for_provenance_only':'C=CC(=O)N'}
    write(paths['source_manifest'], {'schema':'library-eight-md-source-v1','records':[original]})
    row = {**original, 'source_mae_sha256':sha(paths['extracted_pose']), 'sequence_sha256':sha(paths['construct_fasta']),
           'construct_start':696,'construct_end':1016,'missing_internal_residues':[721]}
    write(paths['source_inspection'], {'records':[row],'source_manifest_sha256':sha(paths['source_manifest'])})
    chemical = {'compound_id':compound,'input_sdf_index':'17','standardized_smiles':'C=CC(=O)N',
                'warhead_class':'acrylamide','warhead_count':'1'}
    with paths['candidate_table'].open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(chemical));writer.writeheader();writer.writerow(chemical)
    started = {'system_id':'library_017','source_sha256':sha(paths['extracted_pose']),
               'sequence_sha256':sha(paths['construct_fasta']),'missing_internal_residues':[721]}
    write(paths['loop_started'],started)
    write(paths['loop_finished'],{**started,'returncode':0,'status':'LOOP_MODEL_WRITTEN_REQUIRES_QC',
                                 'output_sha256':sha(paths['filled_pose'])})
    qc = {'system_id':'library_017','source_sha256':sha(paths['extracted_pose']),
          'filled_sha256':sha(paths['filled_pose']),'loop_qc_input_sha256':sha(paths['loop_input']),
          'status':'LOOP_GRAPH_QC_PASSED_PENDING_RELAXATION','warnings':[]}
    write(paths['loop_qc'],qc)
    cleanup = {'system_id':'library_017','source_QC_sha256':sha(paths['loop_qc']),
               'source_input_sha256':sha(paths['loop_input']),'output_sha256':sha(paths['adduct_input'])}
    write(paths['cleanup'],cleanup)
    write(paths['clean_qc'],{**qc,'upstream_loop_qc_sha256':sha(paths['loop_qc']),
        'cleanup_sha256':sha(paths['cleanup']),'loop_qc_input_sha256':sha(paths['adduct_input'])})
    report = {'version':'screening-refinement-report-v1','parent_task_id':task,'workflow_id':workflow,
              'workflow_digest':'4'*64,'candidates':[{'compound_id':compound,'input_sdf_index':17,
                'status':'BOTH_STRUCTURAL_QC_PASSED','af3':{'status':'SUCCESS','quality':{'status':'PASSED'}},
                'covdock':{'status':'SUCCESS','quality':{'status':'PASSED'},'structure_index':32,'structure':'pose.maegz'}}],
              'stages':[{'engine':'covdock','task_id':refinement,'artifacts':[
                  {'name':name,'remote_path':origins[role],'sha256':sha(paths[role])}
                  for role,name in [('pose_file','pose.maegz'),('candidate_table','core_subset_identity.csv')]]}]}
    write(paths['native_report'],report)
    write(paths['native_readback'],{'compute_submitted':False,'execution_records_unchanged':True,
        'result':{'verification':'verified','report_ready':True,'compute_restarted':False,
                  'task_id':task,'workflow_id':workflow,'report':report}})
    write(paths['native_delivery'],{'schema':'refinement-publication-observation-v1','state':'COMPLETE',
        'verification':'verified','workflow_id':workflow,'workflow_digest':'4'*64,'task_id':task,
        'compute_submitted':False,'platform_files_published':False,'files':[
            {'name':name,'sha256':sha(paths[role]),'bytes':paths[role].stat().st_size,
             'artifact_id':role,'version_id':role+'-v1','project_id':'fixture'}
            for role,name in [('pose_file','covdock/pose.maegz'),('candidate_table','covdock/core_subset_identity.csv'),
                              ('native_report','report/refinement-report.json')]]})
    return paths, origins


def intake(root, library):
    paths, origins = library
    return freeze(root, paths, original_paths=origins, system_id='library_017', compound_id='fixture-017', input_sdf_index=17)


def selected(root):
    result = dispatch(root, ENTRY, {'action':'start','request_id':'library-history','target':'EGFR','taxon':9606})
    result = call(root,result,RESEARCH,'resolve_target',target_artifact_id=TARGET_ID,
                  target_manifest_sha256=sha(root/'state/target_identity'/TARGET_SUFFIX/'target_identity.json'))
    return call(root,result,ENTRY,'select_site',site='C797',reason='Review retained Library input with new native checks')


def attached(root, library):
    artifact = intake(root,library)
    return call(root,selected(root),RESEARCH,'attach_historical_md_source',source_artifact_id=artifact['artifact_id'],
        source_manifest_sha256=artifact['manifest_sha256'],reason='Continue a retained source without adopting any old job')


def test_public_intake_freezes_correct_route_and_keeps_new_jobs_unsubmitted(tmp_path, library):
    jobs = {str(p.relative_to(tmp_path)): p.read_bytes() for p in (tmp_path/'state/jobs').rglob('*') if p.is_file()}
    result = attached(tmp_path,library)
    assert result['workflow']['stage'] == 'HISTORICAL_MD_SOURCE_REVIEW'
    assert not result['historical_md_source']['canonical_scope_verified']
    result = call(tmp_path,result,RESEARCH,'prepare_historical_md_candidate')
    state = load(state_file(tmp_path,result))
    pose = load_candidate_pose_input(tmp_path,state['md_input']['artifact_id'],state['md_input']['manifest_sha256'])
    assert pose['manifest']['origin']['route'] == 'library-first'
    assert result['historical_md_source']['canonical_scope_verified']
    assert 'plan_md_admission' in result['workflow']['tools'][RESEARCH]['actions']
    assert 'reconsider' not in result['workflow']['tools'][ENTRY]['actions']
    assert result['execution']['state'] == 'not_submitted' and result['execution']['task_ids'] == []
    assert state['preparation'] is None and not state.get('screening_handoff') and not state.get('refinement_handoff')
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in (tmp_path/'state/jobs').rglob('*') if p.is_file()} == jobs
    assert not result['md_candidate']['md_ready'] and not pose['manifest']['native_graph_qc_completed']


@pytest.mark.parametrize('role,key,value',[
    ('native_delivery','verification','verification_unavailable'),
    ('native_delivery','workflow_id','refineflow-'+'9'*24),
    ('native_readback','execution_records_unchanged',False),
    ('cleanup','output_sha256','0'*64),
    ('loop_finished','returncode',1),
])
def test_broken_provenance_cannot_be_frozen(tmp_path, library, role, key, value):
    path=library[0][role];data=json.loads(path.read_text());data[key]=value;write(path,data)
    with pytest.raises(ValueError):intake(tmp_path,library)
    assert not (tmp_path/'state/historical_md_sources').exists()


def test_changed_frozen_source_is_visible_and_blocks_planning(tmp_path, library):
    result=attached(tmp_path,library)
    artifact=result['historical_md_source'];folder=tmp_path/'state/historical_md_sources'/artifact['manifest_sha256'][:32]
    (folder/'adduct-input.mae').write_text('changed retained input')
    result=dispatch(tmp_path,ENTRY,{'action':'status','workflow_id':result['workflow']['workflow_id']})
    assert result['historical_md_source']['verification']=='evidence_rejected'
    assert 'prepare_historical_md_candidate' not in result['workflow']['tools'][RESEARCH]['actions']
    assert any(b['code']=='evidence_rejected' for b in result['blockers'])


def test_source_hash_does_not_adopt_a_different_target_or_site(tmp_path, library):
    result=attached(tmp_path,library)
    state=load(state_file(tmp_path,result))
    state['site_selection']['site']='C775'
    from covscreen_library_first.historical_md import validate
    with pytest.raises(ValueError,match='site'):validate(tmp_path,state)


def test_unconsumed_source_can_be_archived_but_never_relabelled_as_current_result(tmp_path, library):
    result=attached(tmp_path,library)
    result=call(tmp_path,result,ENTRY,'reconsider',return_to='structure',reason='Preserve source and choose another input')
    assert result['historical_md_source'] is None
    assert any(b.get('historical_md_source') for b in result['branches'])
    assert result['workflow']['stage']=='STRUCTURE_REVIEW'


def test_library_source_cannot_be_attached_as_a_ligand_source(tmp_path, library):
    from covscreen.ligand_workflow import navigate
    artifact = intake(tmp_path,library)
    result = navigate(tmp_path,'start',request_id='wrong-route',target='EGFR',site='C797',taxon=9606)
    with pytest.raises(ValueError,match='historical Ligand source'):
        navigate(tmp_path,'attach_md_source',workflow_id=result['workflow']['workflow_id'],
                 expected_revision=result['workflow']['revision'],source_artifact_id=artifact['artifact_id'],
                 source_manifest_sha256=artifact['manifest_sha256'],reason='A Library input cannot change producer route')
