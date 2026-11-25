"""Fail closed for the De novo local CovDock path with escaped JobControl jobs.

The worker's process-group timeout does not contain detached vendor jobs.
Keep historical plans/results readable; do not admit new local execution until
vendor ownership, recursive termination and terminal reconciliation are verified.
This guard is not itself a cancellation implementation.
"""


def require_budget_control(request, backend):
    if (request.route == 'de-novo' and request.engine == 'schrodinger-covdock-lo'
            and backend == 'local'):
        raise ValueError(
            'De novo local CovDock execution is suspended: vendor JobControl '
            'descendants can outlive the approved timeout; verified vendor '
            'termination and reconciliation are required before new execution'
        )
    return backend
