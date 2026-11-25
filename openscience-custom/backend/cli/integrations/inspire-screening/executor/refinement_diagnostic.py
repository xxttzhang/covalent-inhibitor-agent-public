"""Bounded environment admission evidence, not GPU/scientific acceptance.

Host embeds runtime_probe.py before this module. No molecular engine invocation,
installation, resource creation, or license-content output is permitted here.
"""
import base64
import signal
import sys
import types
import re


def fingerprint(value, limit=2 * 1024 ** 3):
    path = Path(value)
    try:
        before = path.stat()
        if not path.is_file() or not 0 < before.st_size <= limit:
            return {'path': str(path), 'state': 'INVALID_FILE', 'bytes': None, 'sha256': None}
        checksum = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                checksum.update(chunk)
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            return {'path': str(path), 'state': 'CHANGED', 'bytes': None, 'sha256': None}
        return {'path': str(path), 'state': 'HASHED', 'bytes': before.st_size, 'sha256': checksum.hexdigest()}
    except OSError:
        return {'path': str(path), 'state': 'UNAVAILABLE', 'bytes': None, 'sha256': None}


def bounded_process(command, env, cwd, timeout=15):
    # Terminate the whole diagnostic group on timeout, including a vendor wrapper
    # child. Never use lictest's pause/background options or retain a checkout.
    try:
        process = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError:
        return {'state': 'UNAVAILABLE', 'returncode': None, 'output': ''}
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        return {'state': 'TIMEOUT', 'returncode': process.returncode, 'output': ''}
    text = stdout + '\n' + stderr
    if len(text.encode()) > 512 * 1024:
        return {'state': 'INVALID_OUTPUT', 'returncode': process.returncode, 'output': ''}
    return {'state': 'RETURNED', 'returncode': process.returncode, 'output': text.strip()}


def license_probe(root, license_file, env):
    raw = bounded_process([str(root / 'utilities/lictest'), '-f', str(license_file), '-l', 'MMLIBS'], env, str(root))
    # Positive exit alone cannot verify CovDock entitlement; only report this
    # prerequisite's status. Raw stdout/stderr (possibly secrets) never leaves.
    text = raw['output'].lower()
    code = 'COVDOCK_ENTITLEMENT_NOT_TESTED'
    state = 'UNVERIFIED'
    if raw['state'] != 'RETURNED':
        code = 'LICENSE_DIAGNOSTIC_' + raw['state']
    elif 'invalid license file syntax' in text:
        code, state = 'INVALID_LICENSE_SYNTAX', 'FAILED'
    elif 'no features found' in text:
        code, state = 'NO_LICENSE_FEATURES', 'FAILED'
    elif raw['returncode'] != 0 or re.search(r'\b(error|unable|failed)\b', text):
        code, state = 'LICENSE_CHECKOUT_FAILED', 'FAILED'
    return {'state': state, 'code': code, 'feature': 'MMLIBS', 'returncode': raw['returncode'],
            'covdock_entitlement_verified': False}


def license_configuration(science, license_file):
    # Called only after immutable release verification. Use the same guard as
    # LigPrep/CovDock; never implement a more permissive pre-approval substitute.
    source = Path(science) / 'src/covscreen/schrodinger.py'
    try:
        if source.is_symlink() or source.stat().st_size > 128 * 1024:
            raise ValueError('Invalid registered guard')
        module = types.ModuleType('registered_license_configuration_guard')
        module.__file__ = str(source)
        exec(compile(source.read_bytes(), str(source), 'exec'), module.__dict__)
        allowed, _ = module.validate_license_config(str(license_file))
        if type(allowed) is not bool:
            raise ValueError('Invalid guard response')
        return None if allowed else 'LICENSE_CONFIGURATION_REVIEW_REQUIRED'
    except Exception:
        return 'LICENSE_CONFIGURATION_UNAVAILABLE'


ENTITLEMENT_CODE = '''import hashlib,json,pathlib,sys
from schrodinger.application.prime.packages import covalent_docking
from schrodinger.utils import license
source=pathlib.Path(covalent_docking.__file__).resolve(strict=True)
root=pathlib.Path(sys.argv[1]).resolve(strict=True)
source.relative_to(root)
if source.stat().st_size>2*1024*1024:
    raise ValueError('Unexpected vendor module size')
before=hashlib.sha256(source.read_bytes()).hexdigest()
available=False
try:
    # Inspected 2021-2 official method only tests Glide/MacroModel/Prime.
    # No launcher construction, launch(), docking or output files.
    covalent_docking.CovalentDocking.checkLicense(None)
    available=True
except RuntimeError:
    pass
ligprep=bool(license.is_license_available(license.LIGPREP_MAIN))
if before!=hashlib.sha256(source.read_bytes()).hexdigest():
    raise ValueError('Vendor source changed')
print('COVDOCK_ENTITLEMENTS='+json.dumps({'covdock_available':available,'ligprep_available':ligprep,
    'source_path':str(source),'source_sha256':before}))
'''


def entitlement_probe(runtime, env):
    result = {'state': 'UNVERIFIED', 'covdock_available': False, 'ligprep_available': False,
              'source_path': None, 'source_sha256': None, 'returncode': None,
              'scope': 'vendor_covdock_checkLicense_and_ligprep_availability', 'seconds_limit': 60}
    root = Path(runtime['schrodinger_root'])
    raw = bounded_process([str(root / 'run'), 'python3', '-c', ENTITLEMENT_CODE, str(root)], env, str(root), timeout=60)
    result['diagnostic_state'] = raw['state']
    result['returncode'] = raw['returncode']
    if raw['state'] != 'RETURNED' or raw['returncode'] != 0:
        return result
    try:
        lines = [line[len('COVDOCK_ENTITLEMENTS='):] for line in raw['output'].splitlines() if line.startswith('COVDOCK_ENTITLEMENTS=')]
        if len(lines) != 1:
            raise ValueError('Missing or duplicate entitlement evidence')
        value = json.loads(lines[0])
        if (set(value) != {'covdock_available', 'ligprep_available', 'source_path', 'source_sha256'} or
            type(value['covdock_available']) is not bool or type(value['ligprep_available']) is not bool):
            raise ValueError('Invalid entitlement evidence')
        source = Path(value['source_path'])
        source.relative_to(root)
        if source.resolve(strict=True) != source:
            raise ValueError('Redirected vendor source')
        actual = fingerprint(source, limit=2 * 1024 * 1024)
        if actual['state'] != 'HASHED' or actual['sha256'] != value['source_sha256']:
            raise ValueError('Vendor source changed')
        return {**result, **value, 'state': 'PASSED' if value['covdock_available'] and value['ligprep_available'] else 'UNVERIFIED'}
    except (ValueError, TypeError, KeyError, OSError):
        return result


WEIGHTS_CODE = '''import hashlib,json,os,pathlib,resource,sys
# Parameter deserialization only: no model construction, JIT or inference.
resource.setrlimit(resource.RLIMIT_AS,(16*1024**3,16*1024**3))
resource.setrlimit(resource.RLIMIT_CPU,(60,60))
if hasattr(os,'sched_getaffinity'):
    os.sched_setaffinity(0,sorted(os.sched_getaffinity(0))[:2])
phase='imports'
identity={'loader_path':None,'loader_sha256':None}
try:
    import jax,numpy as np
    from alphafold3.model import params
    phase='loader_identity'
    source=pathlib.Path(params.__file__).resolve(strict=True)
    checkout=pathlib.Path(sys.argv[2]).parent/'src/alphafold3/model/params.py'
    installed=pathlib.Path(sys.executable).parent.parent/'lib'/('python%d.%d'%sys.version_info[:2])/'site-packages/alphafold3/model/params.py'
    if source not in (checkout,installed) or source.stat().st_size>1024*1024:
        raise ValueError('Loader source mismatch')
    identity={'loader_path':str(source),'loader_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}
    directory=pathlib.Path(sys.argv[1])
    phase='model_selection'
    files,compressed=params.select_model_files(directory)
    if compressed or files!=[directory/'af3.bin']:
        raise ValueError('Unregistered model selection')
    phase='cpu_backend'
    if any(device.platform!='cpu' for device in jax.devices()):
        raise ValueError('CPU-only diagnostic required')
    phase='parameter_loading'
    loaded=params.get_model_haiku_params(directory)
    phase='parameter_validation'
    schema=[]
    total=0
    for scope,values in sorted(loaded.items()):
        for name,value in sorted(values.items()):
            array=np.asarray(value)
            if not array.size or not np.isfinite(array).all():
                raise ValueError('Empty or nonfinite parameter')
            schema.append([scope,name,list(array.shape),str(array.dtype)])
            total+=array.nbytes
    if not schema or total<=0 or total>2*1024**3:
        raise ValueError('Invalid parameter inventory')
    phase='source_recheck'
    if hashlib.sha256(source.read_bytes()).hexdigest()!=identity['loader_sha256'] or params.select_model_files(directory)!=(files,False):
        raise ValueError('Loader or model selection changed')
    result={'state':'PASSED','code':'CPU_PARAMETERS_LOADED','tensor_count':len(schema),'parameter_bytes':total,
        'schema_sha256':hashlib.sha256(json.dumps(schema,separators=(',',':')).encode()).hexdigest(),'error_type':None,'phase':'completed'}
except Exception as error:
    result={'state':'UNVERIFIED','code':'CPU_PARAMETERS_LOAD_FAILED','tensor_count':None,'parameter_bytes':None,
        'schema_sha256':None,'error_type':type(error).__name__,'phase':phase}
print('AF3_CPU_PARAMETERS='+json.dumps({**result,**identity}))
'''


def weights_probe(runtime):
    result = {'state': 'UNVERIFIED', 'code': 'CPU_PARAMETERS_LOAD_FAILED', 'tensor_count': None,
              'parameter_bytes': None, 'schema_sha256': None, 'error_type': None,
              'loader_path': None, 'loader_sha256': None, 'seconds_limit': 60,
              'memory_limit_bytes': 16 * 1024 ** 3, 'returncode': None}
    result['phase'] = 'not_started'
    python = Path(runtime['af3_python'])
    env = environment()
    env['LD_LIBRARY_PATH'] = str(python.parent.parent / 'lib') + ':/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu'
    raw = bounded_process([str(python), '-I', '-B', '-c', WEIGHTS_CODE, runtime['model_dir'],
                           runtime['runner']], env, str(Path(runtime['runner']).parent), timeout=60)
    result['returncode'] = raw['returncode']
    if raw['state'] != 'RETURNED' or raw['returncode'] != 0:
        result['code'] = 'CPU_PARAMETERS_' + (raw['state'] if raw['state'] != 'RETURNED' else 'LOAD_FAILED')
        return result
    try:
        lines = [line[len('AF3_CPU_PARAMETERS='):] for line in raw['output'].splitlines() if line.startswith('AF3_CPU_PARAMETERS=')]
        if len(lines) != 1:
            raise ValueError('Missing or duplicate diagnostic result')
        value = json.loads(lines[0])
        if set(value) != {'state', 'code', 'tensor_count', 'parameter_bytes', 'schema_sha256', 'error_type', 'phase', 'loader_path', 'loader_sha256'}:
            raise ValueError('Unexpected diagnostic fields')
        if value['phase'] not in ('imports', 'loader_identity', 'model_selection', 'cpu_backend', 'parameter_loading',
                                  'parameter_validation', 'source_recheck', 'completed'):
            raise ValueError('Unknown diagnostic phase')
        passed = value['state'] == 'PASSED' and value['code'] == 'CPU_PARAMETERS_LOADED'
        if passed:
            if (type(value['tensor_count']) is not int or not 0 < value['tensor_count'] <= 100000 or
                type(value['parameter_bytes']) is not int or not 0 < value['parameter_bytes'] <= 2 * 1024 ** 3 or
                not isinstance(value['schema_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', value['schema_sha256']) or
                value['error_type'] is not None or value['phase'] != 'completed'):
                raise ValueError('Invalid parameter inventory')
        elif (value['state'] != 'UNVERIFIED' or value['code'] != 'CPU_PARAMETERS_LOAD_FAILED' or
              any(value[name] is not None for name in ('tensor_count', 'parameter_bytes', 'schema_sha256')) or
              value['phase'] == 'completed' or not isinstance(value['error_type'], str) or not re.fullmatch('[A-Za-z0-9_]{1,80}', value['error_type'])):
            raise ValueError('Invalid failure result')
        if value['loader_path'] is not None:
            source = Path(value['loader_path'])
            checkout = Path(runtime['runner']).parent / 'src/alphafold3/model/params.py'
            installed = re.fullmatch(re.escape(str(python.parent.parent)) + r'/lib/python3\.[0-9]+/site-packages/alphafold3/model/params\.py', str(source))
            if (source != checkout and not installed) or source.resolve(strict=True) != source:
                raise ValueError('Unregistered loader path')
            actual = fingerprint(source, limit=1024 * 1024)
            if actual['state'] != 'HASHED' or actual['sha256'] != value['loader_sha256']:
                raise ValueError('Loader changed during check')
        elif passed or value['loader_sha256'] is not None:
            raise ValueError('Missing loader identity')
        return {**result, **value}
    except (ValueError, TypeError, KeyError, OSError):
        return {**result, 'code': 'CPU_PARAMETERS_INVALID_OUTPUT'}


RECEPTOR_CODE = '''import json,sys
from schrodinger import structure
chain,number=sys.argv[2].split(':')
reader=structure.StructureReader(sys.argv[1])
st=next(reader)
extra=next(reader,None)
reader.close()
atoms=[a for a in st.atom if a.chain.strip()==chain and a.resnum==int(number)]
sg=[a for a in atoms if a.pdbname.strip()=='SG' and a.pdbres.strip()=='CYS']
ca=[a for a in atoms if a.pdbname.strip()=='CA' and a.pdbres.strip()=='CYS']
print(json.dumps({'single_structure':extra is None,'cys_sg_count':len(sg),'cys_ca_count':len(ca)}))
'''


def receptor_probe(runtime, env):
    raw = bounded_process([str(Path(runtime['schrodinger_root']) / 'run'), 'python3', '-c', RECEPTOR_CODE,
                           runtime['receptor'], runtime['attachment_residue']], env, runtime['schrodinger_root'])
    try:
        value = json.loads(raw['output'])
        valid = (raw['state'] == 'RETURNED' and raw['returncode'] == 0 and isinstance(value, dict) and
                 set(value) == {'single_structure', 'cys_sg_count', 'cys_ca_count'} and
                 value['single_structure'] is True and type(value['cys_sg_count']) is int and
                 type(value['cys_ca_count']) is int and value['cys_sg_count'] == value['cys_ca_count'] == 1)
    except (ValueError, TypeError):
        valid = False
    return {'state': 'PASSED' if valid else 'UNVERIFIED', 'attachment_residue': runtime['attachment_residue'],
            'code': 'UNIQUE_CYS_SG_CA' if valid else 'RECEPTOR_MAPPING_NOT_VERIFIED'}


def protein_probe(runtime):
    try:
        file = Path(runtime['source_data_json'])
        if file.stat().st_size > 64 * 1024 * 1024:
            raise ValueError('Input exceeds diagnostic bound')
        data = json.loads(file.read_text())
        proteins = [row['protein'] for row in data['sequences'] if 'protein' in row and row['protein']['id'] == runtime['protein_chain']]
        if len(proteins) != 1 or data['dialect'] != 'alphafold3':
            raise ValueError('Target protein identity is ambiguous')
        protein = proteins[0]
        sequence = protein['sequence']
        index = runtime['cysteine_sequence_index']
        if type(index) is not int or not 1 <= index <= len(sequence) or sequence[index - 1] != 'C':
            raise ValueError('Target residue is not cysteine')
        return {'state': 'PASSED', 'code': 'TARGET_CYS_PRESENT', 'sequence_length': len(sequence),
                'cysteine_sequence_index': index, 'unpaired_msa_present': bool(protein.get('unpairedMsa')),
                'paired_msa_present': bool(protein.get('pairedMsa')), 'template_count': len(protein.get('templates', []))}
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'UNVERIFIED', 'code': 'AF3_TARGET_INPUT_NOT_VERIFIED', 'sequence_length': None,
                'cysteine_sequence_index': runtime['cysteine_sequence_index'], 'unpaired_msa_present': False,
                'paired_msa_present': False, 'template_count': 0}


def import_status(result):
    if result.get('passed'):
        reason = 'PASSED'
    elif result.get('error_type') == 'TimeoutExpired':
        reason = 'TIMEOUT'
    elif result.get('returncode'):
        reason = 'PROCESS_ERROR'
    elif result.get('source_error'):
        reason = 'SOURCE_MISMATCH'
    elif result.get('imports'):
        reason = 'IMPORT_ERROR'
    else:
        reason = 'PROBE_ERROR'
    failed = [{'module': name, 'error_type': value.get('error_type', 'UnknownError')}
              for name, value in result.get('imports', {}).items() if not value.get('imported')]
    return {'reason': reason, 'returncode': result.get('returncode'),
            'seconds_limit': result.get('seconds_limit'), 'failed_modules': failed}


def diagnose(packet):
    profile = packet['profile']
    release = Path(packet['root']) / 'releases' / profile['code']['release_id']
    checker = release / 'code_release.py'
    if checker.is_symlink() or checker.stat().st_size > 1024 * 1024 or release.resolve(strict=True) != release:
        raise ValueError('Immutable code checker differs')
    contents = checker.read_bytes()
    if hashlib.sha256(contents).hexdigest() != profile['code']['checker_sha256']:
        raise ValueError('Immutable code checker differs')
    module = types.ModuleType('diagnostic_verified_release')
    module.__file__ = str(checker)
    exec(compile(contents, str(checker), 'exec'), module.__dict__)
    module.verify(release, profile['code']['release_id'], profile['code']['manifest_sha256'])
    science = release / 'science'
    af3, covdock = profile['af3'], profile['covdock']
    paths = [af3['python'], af3['af3_python'], af3['runner'], af3['source_data_json'],
             str(Path(af3['model_dir']) / 'af3.bin'), covdock['receptor'], covdock['license_file'],
             *[str(Path(covdock['schrodinger_root']) / name) for name in ('run', 'ligprep', 'covalent_docking', 'utilities/lictest')]]
    files = [fingerprint(file) for file in paths]
    # inspect is the host-embedded runtime_probe implementation, never code from
    # arbitrary runtime directories. Only its boolean findings are published.
    first = inspect(af3, science)
    second = inspect(covdock, science)
    env = environment()
    env.pop('LD_LIBRARY_PATH', None)
    env.update({'SCHRODINGER': covdock['schrodinger_root'], 'SCHROD_LICENSE_FILE': covdock['license_file'],
                'LD_LIBRARY_PATH': covdock['runtime_lib_dir'] + ':/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu'})
    configuration = license_configuration(science, covdock['license_file'])
    if configuration:
        # Report the local configuration block without invoking vendor programs
        # in an installation awaiting provenance review. No license text escapes.
        license_result = {'state': 'FAILED', 'code': configuration, 'feature': 'MMLIBS',
                          'returncode': None, 'covdock_entitlement_verified': False}
        entitlements = {'state': 'UNVERIFIED', 'covdock_available': False, 'ligprep_available': False,
                        'source_path': None, 'source_sha256': None, 'returncode': None,
                        'diagnostic_state': 'UNAVAILABLE', 'seconds_limit': 60,
                        'scope': 'vendor_covdock_checkLicense_and_ligprep_availability'}
        receptor = {'state': 'UNVERIFIED', 'attachment_residue': covdock['attachment_residue'],
                    'code': 'RECEPTOR_MAPPING_NOT_VERIFIED'}
    else:
        license_result = license_probe(Path(covdock['schrodinger_root']), covdock['license_file'], env)
        entitlements = entitlement_probe(covdock, env)
        receptor = receptor_probe(covdock, env)
    protein = protein_probe(af3)
    weights = weights_probe(af3)
    # An asset changed while a diagnostic was running is not stable evidence.
    after = [fingerprint(file) for file in paths]
    stable = files == after and all(file['state'] == 'HASHED' for file in files)
    blockers = ['AF3_GPU_AND_MODEL_LOADING_NOT_VERIFIED']
    if entitlements['state'] != 'PASSED':
        blockers.append('COVDOCK_ENTITLEMENT_NOT_VERIFIED')
    if not stable:
        blockers.append('ASSETS_MISSING_OR_CHANGED')
    if not first['wrapper_imports']['passed'] or not first['af3_imports']['passed']:
        blockers.append('AF3_IMPORTS_FAILED')
    if not second['wrapper_imports']['passed']:
        blockers.append('COVDOCK_WRAPPER_IMPORTS_FAILED')
    if protein['state'] != 'PASSED':
        blockers.append(protein['code'])
    if weights['state'] != 'PASSED':
        blockers.append(weights['code'])
    if receptor['state'] != 'PASSED':
        blockers.append(receptor['code'])
    if license_result['state'] == 'FAILED' or license_result['code'] != 'COVDOCK_ENTITLEMENT_NOT_TESTED':
        blockers.append(license_result['code'])
    return {'version': 'refinement-environment-diagnostic-v1', 'task_id': packet['task_id'], 'plan_digest': packet['plan_digest'],
            'profile_digest': packet['profile_digest'], 'nonce': packet['nonce'], 'code': profile['code'],
            'files': files, 'assets_stable': stable,
            'af3': {'wrapper_imports_passed': first['wrapper_imports']['passed'],
                    'native_imports_passed': first['af3_imports']['passed'], 'protein': protein,
                    'wrapper_check': import_status(first['wrapper_imports']), 'native_check': import_status(first['af3_imports']),
                    'cpu_parameters': weights,
                    'model_loaded': False, 'gpu_verified': False},
            'covdock': {'wrapper_imports_passed': second['wrapper_imports']['passed'],
                        'entitlements': entitlements,
                        'wrapper_check': import_status(second['wrapper_imports']), 'license': license_result, 'receptor': receptor},
            'blockers': blockers, 'ready_to_submit': False, 'compute_submitted': False,
            'scientific_quality_verified': False, 'resource_approval_granted': False}


if __name__ == '__main__':
    if len(sys.argv) != 2 or len(sys.argv[1]) > 65536:
        raise ValueError('Bounded diagnostic packet required')
    try:
        result = diagnose(json.loads(base64.b64decode(sys.argv[1], validate=True)))
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        # Never publish exception text from third-party code or credential paths.
        print(json.dumps({'error': 'ENVIRONMENT_DIAGNOSTIC_UNAVAILABLE'}))
        raise SystemExit(1)
