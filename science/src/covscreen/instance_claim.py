"""Select an instance supervisor by the registered engine, never a caller label."""
from .jobs import ArtifactRegistry


def inspect_isolated(root, run_id, instance):
    """Keep scientific allocators out of the single-threaded supervisor."""
    import json
    import os
    import subprocess
    import sys
    program = ('import json,sys; from pathlib import Path; '
               'from covscreen.instance_claim import inspect; '
               'print(json.dumps(inspect(Path(sys.argv[1]),sys.argv[2],sys.argv[3])))')
    result = subprocess.run([sys.executable, '-B', '-c', program, str(root), run_id, instance],
        env={**os.environ, 'PYTHONPATH': os.pathsep.join(sys.path)},
        capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError('Isolated instance validation failed: ' + result.stderr[-8000:])
    return json.loads(result.stdout)


def supervisor(root, run_id):
    engine = ArtifactRegistry(root).load_spec(run_id).engine
    if engine == 'de-novo-prereaction-glide':
        from . import instance_glide_claim
        return instance_glide_claim
    if engine == 'schrodinger-covdock-lo':
        from . import instance_lo_claim
        return instance_lo_claim
    if engine == 'schrodinger-covdock-vs':
        from . import instance_vs_claim
        return instance_vs_claim
    raise ValueError('Unsupported instance scientific engine')


def inspect(root, run_id, instance):
    return supervisor(root, run_id).inspect(root, run_id, instance)


def execute(root, run_id, digest, instance):
    return supervisor(root, run_id).execute(root, run_id, digest, instance)
