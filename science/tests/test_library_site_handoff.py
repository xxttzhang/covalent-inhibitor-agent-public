import json
from pathlib import Path

import pytest
from covscreen.library_site_handoff import plan_from_sites
from covscreen.covalent_site_discovery import evaluate_covalent_sites_tool, select_covalent_sites_tool
from test_covalent_site_discovery import prediction, sha


def selected(root, routes=None, receptor_bytes=None):
    run, manifest = prediction(root)
    value = json.loads(manifest.read_text())
    receptor = receptor_bytes if receptor_bytes is not None else b'HEADER fixture receptor\n'
    import hashlib
    value['structure']['input_sha256'] = hashlib.sha256(receptor).hexdigest()
    manifest.write_text(json.dumps(value))
    evaluated = evaluate_covalent_sites_tool(root, {'prediction_run_id':run,
        'prediction_manifest_sha256':sha(manifest)})
    result = select_covalent_sites_tool(root, {'evaluation_artifact_id':evaluated['artifact_id'],
        'evaluation_manifest_sha256':evaluated['manifest_sha256'],
        'canonical_positions':[797], 'routes': routes or ['library-first']})
    args = {'site_selection_artifact_id':result['artifact_id'],
            'site_selection_manifest_sha256':result['manifest_sha256']}
    return args, result


def register(root):
    from test_adaptive_cascade import request, assessment, route
    project = root/'projects/example-cys'
    (project/'plans').mkdir(parents=True)
    (project/'assessment').mkdir()
    passport = json.loads((Path(__file__).parents[1]/'projects/egfr-c797/target_passport.json').read_text())
    passport['site'].update(site_id='EGFR_C797', residue_number=773, chain_id='A', reactive_atom='SG')
    (project/'target_passport.json').write_text(json.dumps(passport))
    (project/'receptor.pdb').write_text('HEADER fixture receptor\n')
    (project/'library.csv').write_text('compound_id,standardized_smiles\na,CC\nb,CCC\n')
    (project/'assessment/covalent_assessment.json').write_text(assessment().model_dump_json())
    (project/'plans/route_plan.json').write_text(route().model_dump_json())
    req=request().model_copy(update={'site_id':'EGFR_C797','residue_number':773,
        'receptor_path':'receptor.pdb','ligand_table_path':'library.csv'})
    path=project/'plans/library_first_request.json'
    path.write_text(req.model_dump_json())
    return path


def test_missing_inputs_are_structured_and_do_not_write_or_guess(tmp_path):
    args, selected_result=selected(tmp_path)
    before=set(tmp_path.rglob('*'))
    result=plan_from_sites(tmp_path,args)
    assert selected_result['next_calls'][0]['arguments']==args
    assert result['status']=='BLOCKED'
    assert result['sites'][0]['state']=='BLOCKED_MISSING_ROUTE_INPUTS'
    assert not result['calculation_submitted']
    assert set(tmp_path.rglob('*'))==before


def test_registered_request_uses_real_planner_and_counts_real_library(tmp_path,monkeypatch):
    from test_adaptive_cascade import availability
    from covscreen import tool_api
    monkeypatch.setattr(tool_api,'infer_engine_availability',lambda root: availability())
    args,_=selected(tmp_path)
    register(tmp_path)
    result=plan_from_sites(tmp_path,args)
    assert result['status']=='PLANNED'
    assert result['sites'][0]['cascade']['plan']['library_size']==2
    assert not result['calculation_submitted']


@pytest.mark.parametrize('change',['checksum','route','parent','site','path','extra'])
def test_rejects_invalid_identity(tmp_path,change):
    args,_=selected(tmp_path, ['ligand-first'] if change=='route' else None)
    if change=='checksum':args['site_selection_manifest_sha256']='0'*64
    if change=='path':args['site_selection_artifact_id']='../../escape'
    if change=='extra':args['human_approved']=True
    if change in ('parent','site'):
        path=next(tmp_path.glob('state/covalent_site_selection/*/site-selection.json'))
        doc=json.loads(path.read_text())
        if change=='parent':doc['target']['gene']='KRAS'
        else:doc['selected_sites'][0]['author_residue_id']='797'
        path.write_text(json.dumps(doc));args['site_selection_manifest_sha256']=sha(path)
    with pytest.raises(ValueError):plan_from_sites(tmp_path,args)


@pytest.mark.parametrize('change',['canonical_as_author','receptor','ambiguous','escape'])
def test_rejects_wrong_request_binding(tmp_path,change):
    args,_=selected(tmp_path);path=register(tmp_path)
    doc=json.loads(path.read_text())
    if change=='canonical_as_author':doc['residue_number']=797
    if change=='receptor':(path.parents[1]/'receptor.pdb').write_text('wrong structure')
    if change=='escape':doc['ligand_table_path']='../../../escape.csv'
    path.write_text(json.dumps(doc))
    if change=='ambiguous':(path.parent/'library_other_request.json').write_text(path.read_text())
    if change in ('ambiguous','escape'):
        with pytest.raises(ValueError):plan_from_sites(tmp_path,args)
    else:assert plan_from_sites(tmp_path,args)['status']=='BLOCKED'
