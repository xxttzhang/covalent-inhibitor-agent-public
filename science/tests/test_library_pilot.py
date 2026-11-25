import json
import sys
from pathlib import Path
import pytest
import yaml
from covscreen.library_pilot import prepare_pilot
from covscreen.execution import JobManager
from covscreen.jobs import JobSubmissionRequest
from test_library_site_handoff import selected


def environment(root):
    source=root/'source.csv';source.write_text('Compound,SMILES\nfixture,CC\n')
    (root/'configs').mkdir()
    (root/'configs/local.inspire.yaml').write_text(yaml.safe_dump({'covindb':{'virtual_screening_library':str(source)},'runtime':{'python':sys.executable}}))
    return source


def test_pilot_prepares_exact_request_without_computation(tmp_path):
    args,_=selected(tmp_path);environment(tmp_path)
    before=list((tmp_path/'state/jobs').iterdir())
    result=prepare_pilot(tmp_path,{**args,'canonical_position':797})
    assert result['status']=='PLANNED'
    assert not result['calculation_submitted']
    assert result['request']['parameters']['top_n']==10
    assert result['request']['resource_budget']['max_runtime_minutes']==20
    assert '--input-sha256' in result['spec']['command']
    assert list((tmp_path/'state/jobs').iterdir())==before
    repeated=prepare_pilot(tmp_path,{**args,'canonical_position':797})
    assert result['manifest_sha256']==repeated['manifest_sha256']


def test_source_change_invalidates_prepared_request(tmp_path):
    args,_=selected(tmp_path);source=environment(tmp_path)
    result=prepare_pilot(tmp_path,{**args,'canonical_position':797})
    source.write_text(source.read_text()+'extra,CCC\n')
    with pytest.raises(ValueError,match='checksum'):
        JobManager(tmp_path).build_spec(JobSubmissionRequest.model_validate(result['request']))


@pytest.mark.parametrize('extra',[{'canonical_position':781},{'canonical_position':797,'max_rows':9},{'canonical_position':797,'top_n':101},{'canonical_position':797,'top_n':20,'max_rows':10}])
def test_unselected_or_unbounded_pilot_rejected(tmp_path,extra):
    args,_=selected(tmp_path);environment(tmp_path)
    with pytest.raises(ValueError):prepare_pilot(tmp_path,{**args,**extra})


def test_sampling_policy_change_invalidates_frozen_request(tmp_path):
    args,_=selected(tmp_path);environment(tmp_path)
    result=prepare_pilot(tmp_path,{**args,'canonical_position':797})
    request=JobSubmissionRequest.model_validate(result['request'])
    request.parameters['record_selection']='file_order_prefix'
    with pytest.raises(ValueError,match='sampling policy'):
        JobManager(tmp_path).build_spec(request)
