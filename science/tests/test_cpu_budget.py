"""Exercise real Linux thread/process affinity without molecular computation."""
import json
import os
import subprocess
import sys

import pytest

from covscreen.cpu_budget import constrain_cpu_budget


@pytest.mark.parametrize('count', [0, 9, True, 1.5])
def test_invalid_budget_rejected(count):
    with pytest.raises(ValueError, match='CPU thread budget'):
        constrain_cpu_budget(count)


@pytest.mark.skipif(not hasattr(os, 'sched_getaffinity'), reason='Linux affinity contract')
def test_budget_inherited_by_threads_and_child_process():
    program = '''import json,os,subprocess,sys,threading
from covscreen.cpu_budget import constrain_cpu_budget
before=sorted(os.sched_getaffinity(0));result=constrain_cpu_budget(2)
threads=[]
workers=[threading.Thread(target=lambda:threads.append(sorted(os.sched_getaffinity(0)))) for _ in range(6)]
for thread in workers:thread.start()
for thread in workers:thread.join()
child=json.loads(subprocess.check_output([sys.executable,'-B','-c','import json,os; print(json.dumps(sorted(os.sched_getaffinity(0))))']))
assert result['allowed_cpus']==before[:2]
assert len(threads)==6 and all(mask==result['allowed_cpus'] for mask in threads)
assert child==result['allowed_cpus']
assert os.environ['OPENMM_CPU_THREADS']=='2'
assert result['openmm_cpu_threads']==2
print(json.dumps(result))
'''
    result = subprocess.run([sys.executable, '-B', '-c', program],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)['allowed_cpus']) <= 2
