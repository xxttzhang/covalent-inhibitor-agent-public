"""Bounded CPU-only dependency checks, never screening or license checkout.

This is a diagnostic prerequisite, NOT the full engine preflight/approval gate.
Use only host-resolved runtime roles and an independently verified source release.
"""
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path

DATABASES = (
    'bfd-first_non_consensus_sequences.fasta', 'mgy_clusters_2022_05.fa',
    'uniprot_all_2021_04.fa', 'uniref90_2022_05.fa',
    'nt_rna_2023_02_23_clust_seq_id_90_cov_80_rep_seq.fasta',
    'rfam_14_9_clust_seq_id_90_cov_80_rep_seq.fasta',
    'rnacentral_active_seq_id_90_cov_80_linclust.fasta', 'pdb_seqres_2022_09_28.fasta',
)


def environment():
    excluded = {'PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'LD_AUDIT', 'SCHRODINGER',
                'SCHROD_LICENSE_FILE', 'SCHRODINGER_LICENSE_FILE', 'LM_LICENSE_FILE'}
    result = {name: value for name, value in os.environ.items() if name not in excluded}
    result.update({'PYTHONDONTWRITEBYTECODE': '1', 'CUDA_VISIBLE_DEVICES': '', 'JAX_PLATFORMS': 'cpu',
                   'OMP_NUM_THREADS': '1', 'OPENBLAS_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
                   'XLA_PYTHON_CLIENT_PREALLOCATE': 'false'})
    return result


def import_code(modules):
    # No user-supplied Python text. Module names below are chosen by this module.
    return '''import importlib,json,sys
result={}
for name in MODULES:
    try:
        module=importlib.import_module(name)
        result[name]={'imported':True,'file':getattr(module,'__file__',None),'version':getattr(module,'__version__',None)}
    except Exception as error:
        result[name]={'imported':False,'error_type':type(error).__name__,'message':str(error)[:500]}
print(json.dumps({'python':list(sys.version_info[:3]),'executable':sys.executable,'imports':result}))
'''.replace('MODULES', repr(tuple(modules)))


def run(command, env, cwd, timeout=20, modules=None, executable=None):
    try:
        process = subprocess.run(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                 capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {'passed': False, 'error_type': 'TimeoutExpired', 'seconds_limit': timeout}
    except OSError as error:
        return {'passed': False, 'error_type': type(error).__name__}
    if process.returncode:
        # No environment dump, license text or full shell command in evidence.
        return {'passed': False, 'returncode': process.returncode, 'stderr_tail': process.stderr[-1000:]}
    try:
        data = json.loads(process.stdout)
    except (ValueError, TypeError):
        return {'passed': False, 'error_type': 'InvalidDiagnosticJSON'}
    if (not isinstance(data, dict) or set(data) != {'python', 'executable', 'imports'} or
            not isinstance(data['python'], list) or len(data['python']) != 3 or
            any(type(value) is not int or value < 0 for value in data['python']) or
            not isinstance(data['executable'], str) or not Path(data['executable']).is_absolute() or
            not isinstance(data['imports'], dict) or not data['imports'] or
            any(not isinstance(value, dict) or type(value.get('imported')) is not bool for value in data['imports'].values())):
        return {'passed': False, 'error_type': 'InvalidDiagnosticSchema'}
    if modules is not None and set(data['imports']) != set(modules):
        return {**data, 'passed': False, 'source_error': 'Diagnostic module inventory differs from requested imports'}
    if executable is not None and Path(data['executable']).resolve() != Path(executable).resolve():
        return {**data, 'passed': False, 'source_error': 'Interpreter differs from requested runtime'}
    return {**data, 'passed': all(value['imported'] for value in data['imports'].values())}


def metadata(value, checksum=False):
    path = Path(value)
    result = {'path': str(path), 'exists': path.is_file(), 'executable': os.access(path, os.X_OK),
              'resolved': str(path.resolve()), 'bytes': path.stat().st_size if path.is_file() else None}
    if checksum and result['exists']:
        if result['bytes'] > 1024 * 1024:
            result['hash_error'] = 'Bounded configuration file exceeds 1 MiB'
        else:
            result['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def inspect(runtime, science):
    if runtime.get('name') not in ('af3', 'covdock'):
        raise ValueError('Only registered AF3/CovDock dependency diagnostics are supported')
    science = Path(science).resolve(strict=True)
    activation = science / 'environment/activate_carsidock.sh'
    executable = Path(runtime['python'])
    modules = ('numpy', 'pandas', 'pyarrow', 'rdkit', 'covscreen.refinement_input',
               'covscreen.af3_selection' if runtime['name'] == 'af3' else 'covscreen.covdock_gate')
    files = {'wrapper_python': metadata(executable), 'activation': metadata(activation, True)}
    wrapper = run(['bash', '-c', 'set -euo pipefail; unset PYTHONPATH PYTHONHOME LD_PRELOAD; source ' +
        shlex.quote(str(activation)) + ' >/dev/null; exec ' + shlex.quote(str(executable)) + ' -B -c ' +
        shlex.quote(import_code(modules))], environment(), str(science), modules=modules, executable=executable)
    if wrapper.get('passed'):
        for name in modules:
            if not name.startswith('covscreen.'):
                continue
            source = wrapper['imports'][name].get('file')
            if not source or Path(source).resolve() != science / ('src/' + name.replace('.', '/') + '.py'):
                wrapper = {**wrapper, 'passed': False, 'source_error': 'Platform module imported from a different checkout: ' + name}
    result = {'engine': runtime['name'], 'science': str(science), 'files': files, 'wrapper_imports': wrapper,
              'ready': False, 'gpu_verified': False, 'license_checkout_verified': False,
              'model_integrity_verified': False, 'database_integrity_verified': False,
              'resource_approval_granted': False,
              'limitations': ['CPU imports are not GPU compatibility or scientific acceptance.',
                             'Presence/size checks do not verify model or database integrity.',
                             'No licensed executable is launched; entitlement/checkout remains unverified.']}
    if runtime['name'] == 'af3':
        python = Path(runtime['af3_python'])
        prefix = python.parent.parent
        files.update({'af3_python': metadata(python), 'runner': metadata(runtime['runner'], True)})
        env = environment()
        env['LD_LIBRARY_PATH'] = str(prefix / 'lib') + ':/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu'
        native = ('numpy', 'jax', 'jaxlib', 'alphafold3.cpp', 'alphafold3.common.folding_input')
        result['af3_imports'] = run([str(python), '-B', '-c', import_code(native)], env, str(science),
                                    modules=native, executable=python)
        models = sorted(Path(runtime['model_dir']).glob('*.bin'))
        result['model_presence'] = [metadata(path) for path in models[:20]]
        result['model_presence_truncated'] = len(models) > 20
        result['database_presence'] = {name: metadata(Path(runtime['database_dir']) / name) for name in DATABASES}
        return result
    root = Path(runtime['schrodinger_root'])
    files.update({name: metadata(root / name) for name in ('run', 'ligprep', 'covalent_docking')})
    files['license'] = metadata(runtime['license_file'], True)
    result['runtime_lib_dir_present'] = Path(runtime['runtime_lib_dir']).is_dir()
    return result
