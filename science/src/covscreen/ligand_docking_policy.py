"""Versioned Ligand-first routing decisions, independent of compute approval.

Count the entire chemically admitted batch before any screening/top-N selection.
This is a product default, not a calibrated scientific threshold. Historical
receipts retain their original decision; readers must not silently reroute them.
"""
from copy import deepcopy


POLICY = {
    'version': 'ligand-docking-routing-20261111-v1',
    'direct_lo_max_candidates': 6,
    'small_batch_route': 'direct_lo',
    'large_batch_route': 'vs_then_lo',
    'vs_optional_for_small_batch': True,
    'md_source_engine': 'schrodinger-covdock-lo',
}


def decide(candidate_count, *, requested_route='auto', reason=None):
    """Return an auditable route choice; never authorize or claim execution.

Explicit VS is permitted for a small comparative screen with a recorded reason.
A large batch must be screened first under this version of the product policy.
Candidate count is supplied by a validated source, never trusted from the model.
Budget approval and exact prepared-input identity remain downstream contracts.
"""
    if type(candidate_count) is not int or candidate_count < 0:
        raise ValueError('Candidate count must be a nonnegative integer')
    if requested_route not in ('auto', 'direct_lo', 'vs_then_lo'):
        raise ValueError('Unknown Ligand docking route')
    if reason is not None and (not isinstance(reason, str) or not reason.strip()
                               or len(reason.strip()) > 8000):
        raise ValueError('Routing reason must be nonempty text of at most 8000 characters')
    if requested_route != 'auto' and not reason:
        raise ValueError('An explicit routing choice requires a reason')
    small = candidate_count <= POLICY['direct_lo_max_candidates']
    if requested_route == 'direct_lo' and not small:
        raise ValueError('Direct LO exceeds the current small-batch policy')
    route = ('no_candidates' if candidate_count == 0 else
             ('direct_lo' if small else 'vs_then_lo') if requested_route == 'auto'
             else requested_route)
    return {
        'policy': deepcopy(POLICY), 'candidate_count': candidate_count,
        'requested_route': requested_route, 'selected_route': route,
        'selection_reason': reason.strip() if reason else (
            'No chemically admitted candidates; no docking plan.' if not candidate_count else
            'Small batch: predict all admitted candidates with LO.' if small else
            'Large batch: use VS to select a bounded subset for independent LO.'),
        'compute_submitted': False, 'execution_authorized': False,
        'scientific_acceptance': False, 'md_ready': False,
    }
