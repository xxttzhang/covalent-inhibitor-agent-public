"""Return independently verified receptor preparation to a new VS input.

Retain the deposited projection for reference-frame measurements. Only the
vendor input uses prepared coordinates, explicitly renumbered to the same
entity positions; no coordinate fitting or scientific approval occurs here.
"""
import json
import re
from copy import deepcopy
from pathlib import Path

from .jobs import sha256_file
from .ligand_receptor_source import context, pre_vs
from .receptor_evidence import regular


ACTION = 'use_prepared_receptor_for_vs'


def refresh_available(state):
    """Expose explicit recovery only for an invalidated, unsubmitted VS input."""
    from .ligand_vs_input import available
    handoff = context(state)
    record = handoff.get('receptor_preparation') or {}
    return bool(pre_vs(state) and available(state) and handoff.get('use_for_vs')
                and state.get('vs_input_error') and not state.get('vs_execution')
                and record.get('evaluation_verification') == 'verified'
                and record.get('receptor_prepared') is True and not record.get('evidence_error'))


def project(raw, inventory):
    authors = inventory['author_residue_by_entity_position']
    if len(set(authors)) != len(authors):
        raise ValueError('Prepared VS receptor requires unique author residue identities')
    positions = {str(author): i + 1 for i, author in enumerate(authors)}
    if any(not re.fullmatch(r'-?\d+', author) for author in positions):
        raise ValueError('Prepared VS receptor requires an explicit insertion-code adapter')
    expected = {atom['entity_position']: atom['resname'] for atom in inventory['retained_atoms']}
    expected.update({atom['entity_position']: atom['resname'] for atom in inventory['missing_construct_residues']})
    lines, seen, atoms = [], set(), set()
    for line in raw.decode('ascii').splitlines():
        if line[:6].strip() not in {'ATOM', 'HETATM'}:
            continue
        author = str(int(line[22:26])) + line[26:27].strip()
        position = positions.get(author)
        if (line[:6].strip() != 'ATOM' or line[21:22] != inventory['author_chain']
                or line[16:17].strip() or position is None
                or line[17:20].strip() != expected[position]):
            raise ValueError('Prepared VS receptor atom differs from the verified construct')
        identity = (position, line[12:16].strip())
        if identity in atoms:
            raise ValueError('Duplicate prepared VS receptor atom')
        atoms.add(identity)
        seen.add(position)
        lines.append(line[:21] + 'A' + f'{position:4d}' + ' ' + line[27:])
    if seen != set(range(1, len(authors) + 1)):
        raise ValueError('Prepared VS receptor is missing construct residues')
    return ('\n'.join([*lines, 'TER', 'END']) + '\n').encode('ascii')


def augment(root, state, payload, files):
    from .ligand_receptor_execution import observe, review
    from .covdock_receptor import inspect_receptor
    # Downstream LO may have its own receptor record. Revalidate this VS input
    # against its actual original preparation, not that later handoff.
    source = deepcopy({**state, 'refinement_input': None})
    record = context(source).get('receptor_preparation') or {}
    if not record.get('evaluation') or not record.get('evaluation_artifact'):
        raise ValueError('VS receptor replacement requires a verified preparation review')
    output = observe(root, source, record['run_id'])
    review(root, source, output, reuse=True)
    evaluation = record['evaluation']
    if evaluation.get('receptor_prepared') is not True:
        raise ValueError('VS receptor replacement requires current receptor admission, not only a replayed proposal')
    receptor = evaluation['result']['receptor']
    path, _ = regular(root, root / receptor['path'], receptor['sha256'])
    inventory = json.loads(files['receptor-inventory.json'])
    identity = evaluation['result']['identity']
    mapping = payload['source_site_mapping']
    if (identity['canonical_cys'] != mapping['uniprot_residue']
            or str(identity['author_residue']) != mapping['author_residue_id']
            or identity['author_chain'] != mapping['auth_asym_id']):
        raise ValueError('Prepared receptor target differs from the VS study')
    inspect_receptor(path, identity['author_chain'] + ':' + str(identity['author_residue']),
                     expected_sha256=receptor['sha256'])
    files['prepared-vs-receptor.pdb'] = project(path.read_bytes(), inventory)
    witness = record['evaluation_artifact']
    payload['lineage_sha256'].update(evaluation['result']['inventory'])
    payload['lineage_sha256'][witness['path']] = witness['manifest_sha256']
    payload.update(receptor_file='prepared-vs-receptor.pdb', receptor_prepared=True,
        receptor_preparation={'run_id': record['run_id'], 'review': witness, 'receptor': receptor,
            'source_input': source['vs_receptor']['source_input'],
            'projection_implementation_sha256': sha256_file(Path(__file__)),
            'numbering': 'author_to_entity_position_only; coordinates_unchanged'})
    payload['review_requirements'].update(unresolved_residues_rebuilt=True,
        prepared_scope=evaluation['prepared_scope'],
        prepared_site_context={'artifact_id': evaluation['parents']['context_artifact_id'],
                               'manifest_sha256': evaluation['parents']['context_sha256']},
        preparation_is_not_environment_suitability=True)


def lifecycle(root, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response
    from .ligand_parent_evidence import version_lock
    from .workflow_protocol import revision, changed
    from .workflow_store import load
    from .ligand_vs_input import freeze, retire_plan, STAGES
    location = directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, expected_revision)
        handoff = context(state)
        refreshing = refresh_available(state)
        if (arguments or state.get('pending') or not pre_vs(state) or state.get('vs_execution')
                or (not refreshing and (state['stage'] != 'RECEPTOR_PREPARATION_REVIEW'
                                        or handoff.get('use_for_vs')))
                or handoff.get('resume_stage') not in STAGES):
            raise ValueError('Return only the current verified pre-VS receptor to its unsubmitted VS study')
        history = state.get('vs_input_history')
        if history is not None and not isinstance(history, list):
            raise ValueError('VS input history must be a list or null')
        handoff['use_for_vs'] = True
        ref = freeze(root, state)  # Replays registered output and quality; no job.
        old = state['vs_input']
        retire_plan(state, 'Prepared receptor selected; a new environment review and native VS approval are required')
        state['vs_input_history'] = [*(history or []), old]
        state['vs_input'] = ref
        state['stage'] = handoff['resume_stage']
        state.pop('vs_input_error', None)
        state.pop('vs_review_error', None)
        changed(location, state, ACTION)
        return response(state, {'vs_input': ref, 'previous_vs_input': old,
            'environment_review_required': True, 'compute_submitted': False, 'md_ready': False}, compact=True)
