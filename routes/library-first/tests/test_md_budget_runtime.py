"""Budget checks must work without optional jsonschema in the production runtime."""
import sys

import pytest
from covscreen_library_first.md_execution import valid_budget

BASE={'cpu_count':2,'memory_gb':8,'max_runtime_minutes':30,'gpu_count':0,'max_candidates':1}


def test_no_optional_schema_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules,'jsonschema',None)
    assert valid_budget('admission',BASE)
    assert valid_budget('equilibration',{**BASE,'cpu_count':8,'memory_gb':32,'max_runtime_minutes':720,'gpu_count':1})


@pytest.mark.parametrize('patch',[
    {'cpu_count':0},{'cpu_count':5},{'cpu_count':True},{'memory_gb':0},{'memory_gb':17},
    {'memory_gb':float('nan')},{'memory_gb':float('inf')},{'max_runtime_minutes':61},
    {'max_runtime_minutes':1.5},{'gpu_count':1},{'max_candidates':2},
])
def test_graph_budget_rejects_invalid_or_over_limit_values(patch):
    assert not valid_budget('admission',{**BASE,**patch})
