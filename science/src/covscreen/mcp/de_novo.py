"""De novo tool contracts and thin adapters; no scientific execution logic is duplicated."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .annotations import READ_ONLY, EXECUTION
from covscreen.tool_api import (
    plan_de_novo_tool,
)


TOOLS = [
    {
        'name': 'covalent_de_novo_batch',
        'description': 'Prepare, plan, observe and evaluate the registered PocketXMol batch through independent preparation, VS, selected LO and independent Glide SP prereaction planning. Plans return exact native approval arguments and never submit compute. Structure evidence and exploratory rankings do not establish activity or MD readiness.',
        'inputSchema': {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['plan_vs', 'status_vs', 'read_vs_products', 'prepare_lo', 'retry_lo', 'plan_lo', 'status_lo', 'evaluate_lo', 'prepare_prereaction_batch', 'plan_prereaction_batch', 'status_prereaction_batch', 'evaluate_prereaction_batch', 'report_batch']},
            'manifest_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
            'lo_manifest_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
            'prereaction_source_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
            'prereaction_manifest_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
            'cpu_count': {'type': 'integer', 'minimum': 1, 'maximum': 55, 'default': 16},
            'max_runtime_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 240, 'default': 60}},
            'required': ['action', 'manifest_sha256'], 'additionalProperties': False},
        'annotations': EXECUTION,
    },
    {
        'name': 'covalent_de_novo_review',
        'description': ('Plan or read independent CovDock evaluation of a native De novo study source. '
            'The host must first transport its immutable source package to the registered source ID. '
            'One candidate plus an afatinib redocking control; original generation is freshly reverified. '
            'plan freezes the existing shared executor without submitting; returned covalent_submit_job '
            'arguments require separate native approval. cancel_plan prepares only a verified never-dispatched De novo CovDock cancellation; replan freezes its source-preserving second attempt only after native cancellation is confirmed. status observes only that exact request; evaluate '
            'reopens real registered vendor structures and frozen physical gates. Failures stay in the '
            'original study projection. evaluate also measures receptor-aligned control RMSD and checks '
            'the fixed terminal-acrylamide candidate reaction graph and specified stereochemistry. '
            'Only actual registered evidence can pass these checks. plan_prereaction freezes a separate '
            'source-bound noncovalent Vina job for the same candidate and control, requiring its own native approval; '
            'prereaction_status observes that request, evaluate_prereaction remeasures its registered raw poses. '
            'These are exploratory geometry checks, not calibrated reactivity or activity predictions. '
            'prepare_md_handoff stays blocked until all scientific gates and the shared delivery adapter are connected.'),
        'inputSchema': {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['plan', 'replan', 'cancel_plan', 'status', 'evaluate', 'prepare_md_handoff',
                'plan_prereaction', 'prereaction_status', 'evaluate_prereaction']},
            'source_id': {'type': 'string', 'pattern': '^denovo-review-[a-f0-9]{32}$'},
            'source_manifest_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}},
            'required': ['action', 'source_id', 'source_manifest_sha256'], 'additionalProperties': False},
        'annotations': EXECUTION,
    },
    {
        "name": "covalent_plan_de_novo",
        "description": (
            "Build the documented De novo Generation route: pocket definition, pocket-conditioned "
            "3D scaffold generation, target-residue/warhead constraints, anti-reward-hacking "
            "multi-objective optimization, CarsiDock-Cov screening, and candidate-pool handoff. "
            "Reports missing generator assets and never launches a model. PASS validates planning "
            "only, not generated candidates, binding or execution readiness. Caller-supplied "
            "availability is a what-if assertion, never verified runtime evidence or approval."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_path": {"type": "string", "description": "Existing DeNovoRequest inside its registered project; not an arbitrary execution script."},
                "require_inputs": {"type": "boolean", "default": True,
                    "description": "Check declared input paths. False does not accept unverified stage artifacts."},
                "availability": {"type": ["object", "null"],
                    "description": "Optional what-if generator flags. Omit for runtime detection. These flags do not validate checkpoints, scientific outputs or native compute approval."},
            },
            "required": ["request_path"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
]


def handlers(root: Path, arguments: dict[str, Any]) -> dict[str, Callable[[], dict[str, Any]]]:
    return {
        'covalent_de_novo_batch': lambda: batch_review(root, arguments),
        'covalent_de_novo_review': lambda: independent_review(root, arguments),
        'covalent_plan_de_novo': lambda: plan_de_novo_tool(
            root,
            arguments["request_path"],
            require_inputs=arguments.get("require_inputs", True),
            availability=arguments.get("availability"),
        ),
    }


def independent_review(root, arguments):
    from covscreen.layout import enable_de_novo
    enable_de_novo()
    from covscreen_de_novo.independent_review import step
    return step(root, **arguments)


def batch_review(root, arguments):
    import hashlib
    import json
    from covscreen.layout import enable_de_novo
    from covscreen.execution import JobManager
    from covscreen.approval_scope import normalize_spec
    enable_de_novo()
    from covscreen_de_novo.batch_vs import compile_request, status, evaluate
    if arguments['action'] in {'prepare_lo', 'retry_lo', 'plan_lo', 'status_lo', 'evaluate_lo', 'prepare_prereaction_batch', 'plan_prereaction_batch', 'status_prereaction_batch', 'evaluate_prereaction_batch', 'report_batch'}:
        from covscreen_de_novo.batch_lo_input import freeze, load
        if arguments['action'] == 'prepare_lo':
            return freeze(root, arguments['manifest_sha256'])
        digest = arguments.get('lo_manifest_sha256')
        if not digest:
            raise ValueError('Use the frozen LO input manifest returned by prepare_lo')
        payload, _ = load(root, digest)
        if payload['batch_manifest_sha256'] != arguments['manifest_sha256']:
            raise ValueError('Frozen LO input belongs to a different VS batch')
        inputs = {**payload, 'manifest_sha256': digest}
        if arguments['action'] == 'retry_lo':
            from covscreen_de_novo.batch_lo_retry import freeze as freeze_retry
            return freeze_retry(root, digest)
        if arguments['action'] in {'prepare_prereaction_batch', 'plan_prereaction_batch', 'status_prereaction_batch', 'evaluate_prereaction_batch', 'report_batch'}:
            from covscreen_de_novo.batch_prereaction_input import freeze as freeze_prereaction, load as load_prereaction
            if arguments['action'] == 'prepare_prereaction_batch':
                return freeze_prereaction(root, digest)
            source_digest = arguments.get('prereaction_source_sha256')
            manifest_digest = arguments.get('prereaction_manifest_sha256')
            if not source_digest or not manifest_digest:
                raise ValueError('Use the frozen prereaction source and input manifests returned by prepare_prereaction_batch')
            prepared, _ = load_prereaction(root, source_digest, manifest_digest)
            if prepared['source']['lo_manifest_sha256'] != digest:
                raise ValueError('Frozen prereaction input belongs to a different LO batch')
            if arguments['action'] == 'report_batch':
                from covscreen_de_novo.batch_report import report
                return report(root, prepared['source_sha256'], prepared['manifest_sha256'])
            if arguments['action'] in {'status_prereaction_batch', 'evaluate_prereaction_batch'}:
                from covscreen_de_novo.batch_glide import observe
                return observe(root, prepared['source_sha256'], prepared['manifest_sha256'],
                    evaluate=arguments['action'] == 'evaluate_prereaction_batch')
            from covscreen_de_novo.batch_glide import plan as plan_glide
            return plan_glide(root, prepared['source_sha256'], prepared['manifest_sha256'])
        if arguments['action'] in {'status_lo', 'evaluate_lo'}:
            from covscreen_de_novo.batch_lo import observe
            return observe(root, inputs['manifest_sha256'], evaluate=arguments['action'] == 'evaluate_lo')
        from covscreen_de_novo.batch_lo import plan
        return plan(root, inputs['manifest_sha256'])
    if arguments['action'] == 'read_vs_products':
        return evaluate(root, arguments['manifest_sha256'])
    if arguments['action'] == 'status_vs':
        return status(root, arguments['manifest_sha256'])
    if arguments['action'] != 'plan_vs':
        raise ValueError('Unsupported batch action')
    manager = JobManager(root)
    request = compile_request(root, arguments['manifest_sha256'], manager.config.get('schrodinger', {}),
        cpu_count=arguments.get('cpu_count', 16), max_runtime_minutes=arguments.get('max_runtime_minutes', 60))
    spec = normalize_spec(manager._prepare_submission(request))
    return {'schema': 'denovo-batch-vs-plan-v1', 'manifest_sha256': arguments['manifest_sha256'],
        'workflow_id': request.project_id, 'spec': spec,
        'spec_sha256': hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'submit_tool': 'covalent_submit_job',
        'submit_arguments': request.model_copy(update={'execution_mode': 'execute'}).model_dump(mode='json'),
        'native_approval_required': True, 'compute_submitted': False}
