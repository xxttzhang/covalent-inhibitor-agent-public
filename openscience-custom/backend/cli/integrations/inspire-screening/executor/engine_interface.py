"""Read-only registered entry-point contract audit; never executes an engine.

Static interface support is necessary, not sufficient, for runtime readiness.
No license contents, molecular tables, model weights or credentials are read.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path

ENTRIES = {'af3': 'run_af3_registered_batch.py', 'covdock': 'run_covdock_lo_registered.py'}
REQUIRED = {'--candidate-table', '--expected-input-count', '--expected-input-sha256', '--expected-identity-sha256'}
ARGUMENTS = ['args.candidate_table', 'args.expected_input_count', 'args.expected_input_sha256', 'args.expected_identity_sha256']


def inspect(project, engine, expected_sha256=None):
    if engine not in ENTRIES:
        raise ValueError('Unregistered refinement engine')
    project = Path(project).resolve(strict=True)
    entry = (project / 'scripts' / ENTRIES[engine]).resolve(strict=True)
    entry.relative_to(project)
    contents = entry.read_bytes()
    checksum = hashlib.sha256(contents).hexdigest()
    if expected_sha256 is not None and checksum != expected_sha256:
        raise ValueError('Frozen refinement entry source changed')
    tree = ast.parse(contents, filename=str(entry))
    flags = {arg.value for node in ast.walk(tree) if isinstance(node, ast.Call) and
             isinstance(node.func, ast.Attribute) and node.func.attr == 'add_argument'
             for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)}
    main = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main']
    guards = [node for node in (main[0].body if len(main) == 1 else []) if isinstance(node, ast.Assign) and
              isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and
              node.value.func.id == 'verify_refinement_input' and
              [ast.unparse(arg) for arg in node.value.args] == ARGUMENTS and not node.value.keywords]
    helper = (project / 'src/covscreen/refinement_input.py').resolve()
    helper.relative_to(project)
    helper_hash = hashlib.sha256(helper.read_bytes()).hexdigest() if helper.is_file() else None
    required = REQUIRED | ({'--input-score-source'} if engine == 'covdock' else set())
    sources = []
    if engine == 'covdock':
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != 'add_argument':
                continue
            if not any(isinstance(arg, ast.Constant) and arg.value == '--input-score-source' for arg in node.args):
                continue
            for keyword in node.keywords:
                if keyword.arg == 'choices' and isinstance(keyword.value, (ast.Tuple, ast.List)):
                    sources = [item.value for item in keyword.value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
    missing = sorted(required - flags)
    reasons = ([f'Missing exact-input CLI options: {", ".join(missing)}'] if missing else [])
    if len(guards) != 1:
        reasons.append('Expected top-level exact-input guard call is absent or ambiguous')
    if helper_hash is None:
        reasons.append('Exact-input guard module is missing')
    if engine == 'covdock' and 'core' not in sources:
        reasons.append('CovDock entry cannot explicitly preserve core input score provenance')
    geometry = []
    structure_readback = False
    quality_policies = []
    reader = project.parent / 'refinement_readback.py'
    output = project / 'src/covscreen/refinement_output.py'
    if engine == 'af3' and reader.is_file() and output.is_file():
        reader_tree = ast.parse(reader.read_bytes())
        helpers = ast.parse(output.read_bytes())
        calls_geometry = any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and
            node.func.attr == 'verify_af3_geometry' for node in ast.walk(reader_tree))
        supports_contract = any(isinstance(node, ast.Constant) and node.value == 'af3-geometry-evidence-v1' for node in ast.walk(reader_tree))
        if calls_geometry and supports_contract and any(isinstance(node, ast.FunctionDef) and node.name == 'verify_af3_geometry' for node in helpers.body):
            geometry = ['af3-geometry-evidence-v1']
    inspector = project / 'scripts/inspect_covdock_output.py'
    if engine == 'covdock' and reader.is_file() and output.is_file() and inspector.is_file():
        reader_tree = ast.parse(reader.read_bytes())
        helpers = ast.parse(output.read_bytes())
        inspector_tree = ast.parse(inspector.read_bytes())
        structure_readback = (
            any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'verify_covdock_structures'
                for node in ast.walk(reader_tree)) and
            any(isinstance(node, ast.FunctionDef) and node.name == 'verify_covdock_structures' for node in helpers.body) and
            any(isinstance(node, ast.Constant) and node.value == '--readback-stdin' for node in ast.walk(inspector_tree)))
    quality = project / 'src/covscreen/refinement_quality.py'
    if reader.is_file() and quality.is_file():
        reader_tree = ast.parse(reader.read_bytes())
        quality_tree = ast.parse(quality.read_bytes())
        if (any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'read_measurements'
                for node in ast.walk(reader_tree)) and
            any(isinstance(node, ast.FunctionDef) and node.name == 'read_measurements' for node in quality_tree.body) and
            any(isinstance(node, ast.Constant) and node.value == 'refinement-structural-qc-v1' for node in ast.walk(quality_tree))):
            quality_policies = ['refinement-structural-qc-v1']
            provisional = 'refinement-structural-qc-v2-provisional'
            if any(isinstance(node, ast.Constant) and node.value == provisional for node in ast.walk(quality_tree)):
                # Advertise the new policy only with its reader/entry wiring.
                # These AST checks still do not certify the licensed runtime.
                supported = engine == 'af3'
                if engine == 'covdock' and output.is_file():
                    helpers = ast.parse(output.read_bytes())
                    required_readers = {'collect_refinement_outputs', 'freeze_refinement_outputs', 'read_refinement_outputs'}
                    readers = {node.name for node in helpers.body if isinstance(node, ast.FunctionDef)
                        and any(arg.arg == 'expected_gate_version' for arg in node.args.kwonlyargs)}
                    calls = {(node.func.id, keyword.arg, ast.unparse(keyword.value)) for node in ast.walk(tree)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) for keyword in node.keywords}
                    supported = ('--gate-algorithm-version' in flags and required_readers <= readers
                        and ('gate_covdock_candidates', 'algorithm_version', 'args.gate_algorithm_version') in calls
                        and ('freeze_refinement_outputs', 'expected_gate_version', 'args.gate_algorithm_version') in calls
                        and any(isinstance(node, ast.Constant) and node.value == provisional for node in ast.walk(reader_tree)))
                if supported:
                    quality_policies.append(provisional)
    return {'engine': engine, 'entry': str(entry), 'entry_sha256': checksum,
            'helper': str(helper), 'helper_sha256': helper_hash,
            'required_options': sorted(required), 'missing_options': missing, 'input_score_sources': sources,
            'geometry_contracts': geometry,
            'original_structure_readback_supported': structure_readback,
            'quality_policies': quality_policies,
            'exact_input_guard_present': len(guards) == 1,
            'passes_static_checks': not reasons, 'blocking_reasons': reasons,
            'runtime_verified': False, 'license_checkout_verified': False, 'resource_approval_granted': False,
            'limitations': ['Static flags/call checks do not prove runtime behavior or GPU compatibility.',
                           'Engine assets and source hashes must be bound to the approved plan.',
                           'Live resource/rate/image and licensed runtime checks remain required.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--engine', choices=tuple(ENTRIES), required=True)
    parser.add_argument('--expected-sha256')
    args = parser.parse_args()
    result = inspect(args.project, args.engine, args.expected_sha256)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if result['passes_static_checks'] else 2)
