# coding: utf-8
"""Validate documented route configuration against actual Pydantic code; no dispatch."""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[5]
repo = root / 'science'
sys.path.insert(0, str(repo / 'src'))
from pydantic import ValidationError
from covscreen.ligand_chain import LigandFirstChainConfig, load_ligand_chain_config

base = load_ligand_chain_config(repo / 'configs/ligand_first/EGFR_C797_erlotinib_v1.yaml')
native = load_ligand_chain_config(repo / 'configs/ligand_first/EGFR_C797_erlotinib_native_validation_v1.yaml')
assert base.enumeration_backend == 'rdkit-branch'
assert native.enumeration_backend == 'upstream-native'
examples = [
    ('registered default', {}, True),
    ('native backend', {'enumeration_backend':'upstream-native'}, True),
    ('unknown field', {'made_up_tool':True}, False),
    ('unsupported backend', {'enumeration_backend':'attachment-planner'}, False),
    ('skip intermediate stages', {'stage_order':['carsidock','pareto']}, False),
    ('valid suffix', {'stage_order':['independent_evaluation','pareto']}, True),
    ('missing final stage', {'stage_order':['structure_intake']}, False),
    ('out of scope seed', {'stage_order':['pareto'], 'seed_run_ids':{'af3':'not-in-scope'}}, False),
    ('unpaired dispatch policy', {'dispatch_policy':'ligand-smoke-v1'}, False),
    ('zero site budget', {'max_sites':0}, False),
    ('impossible coverage', {'min_af3_coverage':1.1}, False),
    ('bounded smoke', {'stage_order':['carsidock','carsidock_selection','af3','covdock_lo','independent_evaluation','pareto'], 'seed_run_ids':{'carsidock_selection':'existing-selection'}, 'dispatch_policy':'ligand-smoke-v1','dispatch_batch':'ligand-smoke-validation','carsidock_top_n':3,'af3_top_n':3,'covdock_top_n':3,'final_n':3,'covdock_cpu_count':4}, True),
]
checked = []
for name, changes, valid in examples:
    try:
        LigandFirstChainConfig.model_validate({**base.model_dump(), **changes})
        success = True
    except ValidationError:
        success = False
    assert success == valid, name
    checked.append({'name':name, 'expected_valid':valid})
result = {'passed':True,'examples':checked,'count':len(checked),'scientific_jobs_submitted':0,
          'scope':'actual LigandFirstChainConfig only; no engine, registry mutation or job submission'}
(root / 'reports/design-route-skills-20261009/ligand-validation.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
