"""Compile read-only approval evidence before the native user confirmation."""
import hashlib
import json
from pathlib import Path


def normalize_spec(spec):
    value = spec.model_dump(mode='json')
    value.pop('created_at', None)
    value['provenance'].pop('native_approval', None)
    return json.loads(json.dumps(value).replace(spec.run_id, '$APPROVED_RUN'))


def approval_scope(root, name, arguments):
    from .execution import JobManager
    from .jobs import JobSubmissionRequest, sha256_file
    from .tool_api import (_adduct_parameterization_request, _openmm_equilibration_request,
                           _openmm_production_request,
                           _trajectory_audit_request,
                           _structural_analysis_request,
                           _md_report_request,
                           _md_candidate_delivery_request,
                           _md_adduct_admission_request,
                           _openmm_short_qc_request,
                           _openmm_system_preparation_request,
                           _receptor_preparation_request, ligand_first_campaign_tool,
                           ligand_first_chain_tool, library_first_chain_tool)
    from .covalent_site_discovery import _prediction_request
    root = Path(root).resolve()
    manager = JobManager(root)
    config = root / 'configs/local.inspire.yaml'
    runtime = sha256_file(config) if config.is_file() else None
    if name == 'covalent_library_research' and arguments.get('action') == 'cancel_md_stage':
        from .layout import enable_library_first
        enable_library_first()
        from covscreen_library_first.md_execution import cancellation_scope
        return cancellation_scope(root, arguments)
    if name == 'covalent_library_research' and arguments.get('action') == 'start_lo':
        from .library_lo_execution import start_request
        request = start_request(root, arguments)
    elif name == 'covalent_library_research' and arguments.get('action') == 'start_vs_prescreen':
        from .library_vs_prescreen_execution import start_request
        request = start_request(root, arguments)
    elif name == 'covalent_library_research' and arguments.get('action') == 'start_vs':
        from .library_vs_execution import start_request
        request = start_request(root, arguments)
    elif name == 'covalent_ligand_design_step' and arguments.get('action') == 'start_vs':
        from .ligand_vs_execution import start_request
        request = start_request(root, arguments)
    elif name == 'covalent_ligand_design_step' and arguments.get('action') == 'start_pose':
        from .ligand_pose_execution import start_request
        request = start_request(root, arguments)
    elif name == 'covalent_ligand_design_step' and arguments.get('action') == 'start_af3':
        from .ligand_af3_execution import start_request
        request = start_request(root, arguments)
    elif name == 'covalent_ligand_design_step' and arguments.get('action') == 'start_receptor_preparation':
        from .ligand_receptor_execution import start_request
        request = start_request(root, arguments)
    elif name == 'covalent_ligand_design_step' and arguments.get('action') == 'start_covdock':
        from .ligand_covdock_execution import start_request
        request = start_request(root, arguments)
    elif name == 'covalent_ligand_design_step' and arguments.get('action') in {'start_md_system_preparation', 'start_md_short_qc', 'start_md_equilibration'}:
        from .ligand_md_preparation import start_request
        request = start_request(root, arguments)
    elif name == 'covalent_ligand_design_step' and arguments.get('action') == 'start_md_parameterization':
        from .ligand_md_parameterization import start_request
        request = start_request(root, arguments)
    elif name == 'covalent_ligand_design_step' and arguments.get('action') == 'start_md_graph':
        from .ligand_md_graph_execution import start_request
        request = start_request(root, arguments)
    elif name == 'covalent_library_research' and arguments.get('action') in {'start_md_admission', 'start_md_parameterization', 'start_md_system_preparation', 'start_md_short_qc', 'start_md_equilibration'}:
        from .layout import enable_library_first
        enable_library_first()
        from covscreen_library_first.md_execution import start_request
        request = start_request(root, arguments)
    elif name == 'covalent_library_receptor':
        from .library_workflow import receptor_start_request
        request = receptor_start_request(root, arguments)
    elif name == 'covalent_predict_cysteine_sites':
        request = _prediction_request(arguments, execution_mode='execute')
    elif name == 'covalent_ligand_first_campaign':
        planned = ligand_first_campaign_tool(root, arguments['config_path'], action='plan',
            max_runtime_minutes=arguments.get('max_runtime_minutes', 240))
        if arguments.get('approved_plan_sha256') != planned['plan_sha256']:
            raise ValueError('Campaign plan changed; request a new plan before approval')
        return {'kind': 'campaign', 'runtime_sha256': runtime, 'plan': planned}
    elif name == 'covalent_library_first_prepare_receptor':
        request = _receptor_preparation_request(root, arguments, execution_mode='execute')
    elif name == 'covalent_library_first_parameterize_adduct':
        request = _adduct_parameterization_request(root, arguments, execution_mode='execute')
    elif name == 'covalent_library_first_prepare_openmm_system':
        request = _openmm_system_preparation_request(root, arguments, execution_mode='execute')
    elif name == 'covalent_library_first_run_openmm_short_qc':
        request = _openmm_short_qc_request(root, arguments, execution_mode='execute')
    elif name == 'covalent_library_first_equilibrate_openmm_system':
        request = _openmm_equilibration_request(root, arguments, execution_mode='execute')
    elif name == 'covalent_library_first_run_openmm_production':
        request = _openmm_production_request(root, arguments, execution_mode='execute')
    elif name == 'covalent_library_first_audit_openmm_trajectory':
        request = _trajectory_audit_request(root, arguments, execution_mode='execute')
    elif name == 'covalent_library_first_analyze_openmm_replicas':
        request = _structural_analysis_request(root, arguments, execution_mode='execute')
    elif name == 'covalent_library_first_build_md_report':
        request = _md_report_request(root, arguments, execution_mode='execute')
    elif name == 'covalent_library_first_deliver_md_candidates':
        request = _md_candidate_delivery_request(root, arguments, execution_mode='execute')
    elif name == 'covalent_library_first_admit_md_candidate':
        request = _md_adduct_admission_request(root, arguments, execution_mode='execute')
    elif name in {'covalent_retry_job', 'covalent_cancel_job'}:
        previous = manager.registry.load_spec(arguments['run_id'])
        if name == 'covalent_cancel_job':
            from .job_cancellation import validate_request
            validate_request(manager.registry, arguments['run_id'])
            if (previous.route == 'ligand-first'
                    and previous.provenance.get('worker_backend') == 'external-hpc'):
                from .scheduler_cancellation import scope
                return scope(manager.registry, previous.run_id)
            return {'kind': 'cancel', 'run_id': previous.run_id,
                    'spec_sha256': sha256_file(manager.registry.job_dir(previous.run_id) / 'spec.json')}
        request = manager.compile_retry_request(
            previous.run_id,
            human_approved=True,
            approval_note=arguments['approval_note'],
            execution_mode=arguments.get('execution_mode', 'execute'),
        )
    elif name in {'covalent_ligand_first_chain', 'covalent_library_first_chain'}:
        function = ligand_first_chain_tool if name == 'covalent_ligand_first_chain' else library_first_chain_tool
        planned = function(root, arguments['config_path'], execution_mode='execute', submit=False,
            human_approved=True, approval_note=arguments['approval_note'])
        if not planned['next_request']:
            raise ValueError('No legal next stage to approve')
        request = JobSubmissionRequest.model_validate(planned['next_request'])
    elif name == 'covalent_submit_job':
        request = JobSubmissionRequest.model_validate(arguments)
    else:
        raise ValueError('Unregistered execution approval')
    spec = manager.build_spec(request)
    spec.provenance['worker_backend'] = manager.worker_backend(request)
    if spec.provenance['worker_backend'] == 'external-openmm':
        from .external_openmm import validate_boundary
        validate_boundary(spec)
    if spec.provenance['worker_backend'] == 'external-inspire':
        from .external_inspire import validate_boundary
        validate_boundary(spec)
    if spec.provenance['worker_backend'] == 'external-hpc':
        from .external_hpc import validate_boundary
        validate_boundary(spec)
    if spec.parameters.get('dispatch_policy') == 'ligand-hpc-retained-v1':
        from .covdock_retained import validate_retained_spec
        validate_retained_spec(root, spec)
    return {'kind': 'job', 'runtime_sha256': runtime, 'spec': normalize_spec(spec)}


def prepare(root, name, arguments):
    scope = approval_scope(root, name, arguments)
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'status': 'NATIVE_APPROVAL_PREPARED', 'scope': scope, 'scope_sha256': digest,
            'compute_submitted': False}
