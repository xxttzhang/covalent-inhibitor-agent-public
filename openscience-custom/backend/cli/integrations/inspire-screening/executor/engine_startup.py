"""Host-embedded admission inside an already approved engine allocation.

No resource creation. No molecular prediction is performed here. Failure must
prevent the actual engine launch; the receipt remains in the same task directory.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time


AF3_STARTUP = '''import hashlib,json,pathlib,sys
import jax
import jax.numpy as jnp
from alphafold3.model import params
packet=json.loads(sys.argv[1])
source=pathlib.Path(params.__file__).resolve(strict=True)
if packet['assets'].get(str(source))!=hashlib.sha256(source.read_bytes()).hexdigest():
    raise ValueError('Unapproved AF3 loader')
files,compressed=params.select_model_files(pathlib.Path(packet['runtime']['model_dir']))
if compressed or files!=[pathlib.Path(packet['runtime']['model_dir'])/'af3.bin']:
    raise ValueError('Unapproved AF3 model selection')
devices=jax.devices('gpu')
if len(devices)!=1 or devices[0].platform!='gpu':
    raise ValueError('Expected the approved single GPU')
with jax.default_device(devices[0]):
    loaded=params.get_model_haiku_params(pathlib.Path(packet['runtime']['model_dir']))
    leaves=jax.tree_util.tree_leaves(loaded)
    if not leaves:
        raise ValueError('Empty AF3 parameters')
    for leaf in leaves:
        leaf.block_until_ready()
        if leaf.devices()!={devices[0]}:
            raise ValueError('Parameters were not placed on the GPU')
    check=jax.jit(lambda x:x+x)(jnp.ones(8)).block_until_ready()
    if check.devices()!={devices[0]} or not bool(jnp.all(check==2)):
        raise ValueError('GPU execution check failed')
print('ENGINE_ADMISSION='+json.dumps({'engine':'af3','state':'PASSED','gpu_count':len(devices),'parameter_tensors':len(leaves)}))
'''

COVDOCK_STARTUP = '''import hashlib,json,pathlib,sys
from schrodinger.application.prime.packages import covalent_docking
from schrodinger.utils import license
packet=json.loads(sys.argv[1])
source=pathlib.Path(covalent_docking.__file__).resolve(strict=True)
if packet['assets'].get(str(source))!=hashlib.sha256(source.read_bytes()).hexdigest():
    raise ValueError('Unapproved CovDock license checker')
covalent_docking.CovalentDocking.checkLicense(None)
if not license.is_license_available(license.LIGPREP_MAIN):
    raise RuntimeError('LigPrep unavailable')
print('ENGINE_ADMISSION='+json.dumps({'engine':'covdock','state':'PASSED','vendor_license_check':True,'ligprep_available':True}))
'''


def engine_admission(plan, env):
    runtime = plan['runtime']
    payload = json.dumps({'runtime': runtime, 'assets': plan['assets']}, separators=(',', ':'))
    clean = {key: value for key, value in env.items() if key not in {
        'PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'SCHRODINGER_PYTHONPATH',
        'SCHRODINGER', 'SCHROD_LICENSE_FILE', 'SCHRODINGER_LICENSE_FILE', 'LM_LICENSE_FILE', 'JAX_PLATFORMS'}}
    if runtime['name'] == 'af3':
        python = Path(runtime['af3_python'])
        clean.update(JAX_PLATFORMS='cuda', XLA_PYTHON_CLIENT_PREALLOCATE='false',
            LD_LIBRARY_PATH=str(python.parent.parent / 'lib') + ':/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu')
        command = [str(python), '-I', '-B', '-c', AF3_STARTUP, payload]
    else:
        clean.update(SCHRODINGER=runtime['schrodinger_root'], SCHROD_LICENSE_FILE=runtime['license_file'],
            LD_LIBRARY_PATH=runtime['runtime_lib_dir'] + ':/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu')
        command = [str(Path(runtime['schrodinger_root']) / 'run'), 'python3', '-c', COVDOCK_STARTUP, payload]
    deadline = min(180, (plan['finish_before'] - int(time.time() * 1000)) / 1000)
    if deadline <= 0:
        raise ValueError('Approved deadline elapsed')
    process = subprocess.Popen(command, env=clean, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        stdout, _ = process.communicate(timeout=deadline)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise ValueError('Runtime admission timed out') from None
    if process.returncode != 0 or len(stdout) > 65536:
        raise ValueError('Runtime admission failed')
    lines = [line[len('ENGINE_ADMISSION='):] for line in stdout.splitlines() if line.startswith('ENGINE_ADMISSION=')]
    if len(lines) != 1:
        raise ValueError('Runtime admission evidence unavailable')
    value = json.loads(lines[0])
    if value.get('engine') != runtime['name'] or value.get('state') != 'PASSED':
        raise ValueError('Runtime admission identity changed')
    if runtime['name'] == 'af3':
        if set(value) != {'engine', 'state', 'gpu_count', 'parameter_tensors'} or value['gpu_count'] != 1 or type(value['parameter_tensors']) is not int or value['parameter_tensors'] <= 0:
            raise ValueError('GPU admission not verified')
    elif set(value) != {'engine', 'state', 'vendor_license_check', 'ligprep_available'} or value['vendor_license_check'] is not True or value['ligprep_available'] is not True:
        raise ValueError('Vendor admission not verified')
    return value


def retain_engine_admission(plan, env, directory):
    receipt = {'version': 'engine-startup-admission-v1', 'task_id': plan['id'], 'engine': plan['engine']['name'],
        'state': 'FAILED', 'at': int(time.time() * 1000), 'scientific_inference_completed': False}
    # Reserve before any process launch, not merely before writing its result.
    with (Path(directory) / 'runtime-admission.json').open('x') as stream:
        try:
            result = engine_admission(plan, env)
            receipt.update(state='PASSED', evidence=result)
        except Exception:
            receipt['error'] = 'ENGINE_STARTUP_ADMISSION_FAILED'
            raise
        finally:
            json.dump(receipt, stream, sort_keys=True)
            stream.write('\n')
