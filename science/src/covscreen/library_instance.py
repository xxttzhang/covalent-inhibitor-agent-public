"""Library CPU instance limits, separate from Ligand and De novo dispatch."""

INSTANCE = 'asgbie2'
LO_POLICY = 'library-lo-instance-v1'


def configured(root):
    from .execution import JobManager
    runtime = JobManager(root).config.get('runtime', {})
    selected = runtime.get('library_instance')
    if selected is not None and selected != INSTANCE:
        raise ValueError('Library execution is restricted to asgbie2')
    return selected, runtime.get('library_cpu_limit', 55)


def budget(count, *, stage, cpu_limit=55):
    limit = {'vs': 2000, 'lo': 100}[stage]
    if type(count) is not int or not 1 <= count <= limit:
        raise ValueError('Library candidate count exceeds the stage budget')
    if type(cpu_limit) is not int or not 1 <= cpu_limit <= 55:
        raise ValueError('Library CPU concurrency must fit asgbie2 allocation')
    cpus = min(count, cpu_limit)
    return {'gpu_count': 0, 'cpu_count': cpus, 'memory_gb': max(8, 4 * cpus),
            'max_runtime_minutes': 360, 'max_candidates': count}


def validate_lo(spec, instance):
    p, b = spec.parameters, spec.resource_budget
    count = p.get('expected_input_count')
    if (instance != INSTANCE or p.get('execution_instance') != INSTANCE
            or spec.route != 'library-first' or spec.engine != 'schrodinger-covdock-lo'
            or p.get('dispatch_policy') != LO_POLICY
            or not spec.project_id.startswith('library-flow-') or p.get('workflow_id') != spec.project_id
            or spec.provenance.get('worker_backend') != 'external-instance'
            or spec.attempt != 1 or spec.parent_run_id is not None
            or b.model_dump(mode='json') != budget(count, stage='lo', cpu_limit=b.cpu_count)
            or p.get('njobs') != b.cpu_count or p.get('host') != f'localhost:{b.cpu_count}'
            or p.get('input_row_count') != count or p.get('final_n') != count
            or p.get('input_score_source') != 'core' or p.get('resume') is not False):
        raise ValueError('Library LO instance recipe differs from its bounded native plan')
