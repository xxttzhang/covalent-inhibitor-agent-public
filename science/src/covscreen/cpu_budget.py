"""Constrain Linux CPU engines before native libraries create worker threads."""
import os


def constrain_cpu_budget(count):
    if type(count) is not int or not 1 <= count <= 8:
        raise ValueError("CPU thread budget must be an integer in [1, 8]")
    if not hasattr(os, "sched_getaffinity") or not hasattr(os, "sched_setaffinity"):
        raise RuntimeError("CPU engine execution requires Linux CPU affinity support")
    # CPU PME has its own pool and does not use Context Threads.
    for name in ("OPENMM_CPU_THREADS", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = str(count)
    allowed = sorted(os.sched_getaffinity(0))
    selected = allowed[:count]
    if not selected:
        raise RuntimeError("No CPU is available within the current affinity mask")
    os.sched_setaffinity(0, selected)
    actual = sorted(os.sched_getaffinity(0))
    if actual != selected:
        raise RuntimeError("CPU affinity could not enforce the approved budget")
    return {"requested_cpus": count, "allowed_cpus": actual, "openmm_cpu_threads": count,
            "enforcement": "linux_affinity_inherited_by_threads_and_children"}
