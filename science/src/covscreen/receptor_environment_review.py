"""Source-bound scientific judgments, distinct from measurements and permission.

The caller revalidates the study input. This shared record verifies provenance
and completeness; it does not prove that cited evidence entails a judgment.
"""
import hashlib
import json
from pathlib import Path
import re

from .jobs import ArtifactRegistry, sha256_file
from .receptor_evidence import regular


CONCERNS = ('assembly', 'excluded_components', 'construct_and_site', 'reaction_scope')
MOTION_CONCERNS = ('observed_displacement', 'target_site_preservation',
                   'modeled_region_context', 'restraint_and_scope_limitations')
DECISIONS = ('suitable_for_exploratory_refinement', 'requires_revision', 'evidence_insufficient')
CITATION_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {'path': {'type': 'string'}, 'sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
        'locator': {'type': 'string', 'minLength': 1, 'maxLength': 500},
        'observation': {'type': 'string', 'minLength': 10, 'maxLength': 2000}},
    'required': ['path', 'sha256', 'locator', 'observation']}
ITEM_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'decision': {'type': 'string', 'enum': list(DECISIONS)},
    'rationale': {'type': 'string', 'minLength': 20, 'maxLength': 4000},
    'evidence': {'type': 'array', 'minItems': 1, 'maxItems': 16, 'items': CITATION_SCHEMA},
    'limitations': {'type': 'array', 'minItems': 1, 'maxItems': 16,
                   'items': {'type': 'string', 'minLength': 5, 'maxLength': 1000}}},
    'required': ['decision', 'rationale', 'evidence', 'limitations']}
REVIEW_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {name: ITEM_SCHEMA for name in CONCERNS}, 'required': list(CONCERNS)}

from copy import deepcopy
MOTION_ITEM_SCHEMA = deepcopy(ITEM_SCHEMA)
MOTION_ITEM_SCHEMA['properties']['decision']['enum'] = [
    'suitable_for_exploratory_receptor_preparation', 'requires_revision', 'evidence_insufficient']
MOTION_REVIEW_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'properties': {name: MOTION_ITEM_SCHEMA for name in MOTION_CONCERNS},
    'required': list(MOTION_CONCERNS)}


def encoded(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()


def text(value, minimum, maximum):
    return isinstance(value, str) and minimum <= len(value.strip()) <= maximum


EVIDENCE_READ_SCHEMA = {
    'evidence_path': {'type': 'string', 'maxLength': 2000},
    'evidence_locator': {'type': 'string', 'maxLength': 500},
    'evidence_offset': {'type': 'integer', 'minimum': 0},
}

READ_GUIDANCE = (
    'Read evidence through the current native reader using evidence_path, evidence_locator and evidence_offset. '
    'Follow next_offset until null and inspect incomplete children before claiming full coverage. '
    'A final page alone is not the whole document. Cite exact returned values. '
    'PDB header_records preserve deposited text with original one-based line numbers; '
    'REMARK 350 may describe assembly, but absence of a record is not evidence of absence of an assembly. '
    'For assembly, inspect the bound source_structures/structure.pdb at /header_records/REMARK 350; '
    'entry.json assembly_ids identify assemblies but do not describe their composition or operators. '
    'One polymer entity can occur in multiple copies: entity count and selected-chain count do not establish monomeric assembly. '
    'For reaction_scope, read input.json /selected_candidate_ids, the matching candidate records and campaign protection details. '
    'A geometry policy or protected_interactions_defined flag does not replace reaction atom mapping or named protected interactions. '
    'Do not infer spatial distance or functional irrelevance of unresolved residues from sequence numbering. '
    'Keep source declarations, measured geometry and scientific judgments distinct. '
    'If evidence cannot support a concern, record evidence_insufficient; do not invent a positive judgment. '
    'Do not read remote paths with local file tools.'
)


def document(raw, name):
    """Expose exact deposited headers, not a predicted assembly or repaired model.

    PDB pointers address this deterministic view of the hash-bound source file.
    Coordinate/connectivity records are intentionally not presented as headers.
    """
    if name.endswith('.json'):
        return json.loads(raw)
    if name.endswith('.pdb'):
        records = {}
        excluded = {'ATOM', 'HETATM', 'ANISOU', 'SIGATM', 'SIGUIJ', 'TER',
                    'MODEL', 'ENDMDL', 'CONECT', 'MASTER', 'END'}
        for number, line in enumerate(raw.decode('utf-8').splitlines(), 1):
            key = line[:6].strip()
            if not key or key in excluded:
                continue
            if key == 'REMARK':
                key += ' ' + line[7:10].strip()
            records.setdefault(key, []).append({'line_number': number, 'text': line})
        return {'format': 'pdb-header-lines-v1', 'header_records': records,
                'interpretation': 'Exact deposited source text, not independently validated biological suitability; '
                    'coordinates and connectivity are omitted from this header view.'}
    raise ValueError('Evidence source must be a bound JSON or PDB file')


def resolve_pointer(value, pointer):
    if pointer == '':
        return value
    if not isinstance(pointer, str) or not pointer.startswith('/') or re.search(r'~(?![01])', pointer):
        raise ValueError('Environment citation locator must be a JSON pointer')
    for part in pointer.split('/')[1:]:
        key = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, dict) and key in value:
            value = value[key]
        elif isinstance(value, list) and re.fullmatch(r'0|[1-9][0-9]*', key) and int(key) < len(value):
            value = value[int(key)]
        else:
            raise ValueError('Environment citation locator is absent from the bound source')
    return value


def read_evidence(root, inventory, *, evidence_path=None, evidence_locator='', evidence_offset=0):
    """Bounded navigation of scientific JSON and PDB headers, never arbitrary files.

    Large nodes expose real child pointers, not invented excerpts. Each request
    rechecks the frozen hash; private execution plans are not in this inventory.
    """
    if (not isinstance(evidence_locator, str) or len(evidence_locator) > 500
            or type(evidence_offset) is not int or evidence_offset < 0):
        raise ValueError('Invalid evidence locator or offset')
    if evidence_path is None:
        if evidence_locator or evidence_offset:
            raise ValueError('Select an evidence_path before a locator or offset')
        return {'documents': [{'path': name, 'sha256': checksum} for name, checksum in inventory.items()
                if name.endswith(('.json', '.pdb'))], 'instruction': READ_GUIDANCE}
    if (not isinstance(evidence_path, str) or evidence_path not in inventory
            or not evidence_path.endswith(('.json', '.pdb'))):
        raise ValueError('Evidence path must be a JSON or PDB file in the current bound inventory')
    path, _ = regular(root, root / evidence_path, inventory[evidence_path])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != inventory[evidence_path]:
        raise ValueError('Environment evidence changed during reading')
    value = resolve_pointer(document(raw, evidence_path), evidence_locator)
    result = {'path': evidence_path, 'sha256': inventory[evidence_path], 'locator': evidence_locator,
              'offset': evidence_offset, 'complete': False}
    if len(encoded(value)) <= 8192:
        if evidence_offset:
            raise ValueError('Complete evidence node has no continuation offset')
        return {**result, 'complete': True, 'value': value, 'next_offset': None}
    if isinstance(value, (dict, list)):
        keys = list(value) if isinstance(value, dict) else list(range(len(value)))
        if evidence_offset >= len(keys):
            raise ValueError('Evidence offset exceeds node length')
        children, size = [], 0
        for key in keys[evidence_offset:evidence_offset + 20]:
            child = value[key]
            pointer = evidence_locator + '/' + str(key).replace('~', '~0').replace('/', '~1')
            item = {'locator': pointer, 'type': type(child).__name__}
            # Return whole records when a page can hold them. A small fixed
            # child cutoff forced an extra model round trip for every candidate.
            # Oversized records still expose a navigable pointer, never a
            # truncated value labelled complete.
            if len(encoded({**item, 'value': child, 'complete': True})) <= 6000:
                item.update(value=child, complete=True)
            else:
                item.update(complete=False, length=len(child) if isinstance(child, (dict, list, str)) else None)
            if children and size + len(encoded(item)) > 6000:
                break
            children.append(item)
            size += len(encoded(item))
        following = evidence_offset + len(children)
        return {**result, 'children': children, 'length': len(keys),
                'coverage': {'first_child_offset': evidence_offset, 'end_child_offset_exclusive': following,
                             'complete_child_values': sum(item['complete'] for item in children),
                             'total_children': len(keys), 'whole_node_returned': False},
                'next_offset': following if following < len(keys) else None}
    if isinstance(value, str):
        if evidence_offset >= len(value):
            raise ValueError('Evidence offset exceeds node length')
        following = min(evidence_offset + 1024, len(value))
        return {**result, 'value_fragment': value[evidence_offset:following], 'length': len(value),
                'next_offset': following if following < len(value) else None}
    raise ValueError('Evidence scalar exceeds the response limit')


def cited_value(root, citation):
    """Resolve a pointer into frozen JSON/PDB header view, never a claimed excerpt."""
    path, _ = regular(root, root / citation['path'], citation['sha256'])
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != citation['sha256']:
        raise ValueError('Environment citation changed during reading')
    try:
        value = document(raw, citation['path'])
    except (ValueError, UnicodeDecodeError) as error:
        raise ValueError('Environment citations require valid JSON or PDB evidence') from error
    pointer = citation['locator']
    if not pointer.startswith('/'):
        raise ValueError('Environment citation locator must be a JSON pointer')
    value = resolve_pointer(value, pointer)
    if len(encoded(value)) > 16384:
        raise ValueError('Environment citation is too broad; select a more specific JSON field')
    return value


def positive_decision(scope):
    if scope == 'restrained-receptor':
        return 'suitable_for_exploratory_receptor_preparation'
    if scope not in {'refinement', 'covdock-vs'}:
        raise ValueError('Unsupported environment review scope')
    return ('suitable_for_exploratory_vs' if scope == 'covdock-vs'
            else 'suitable_for_exploratory_refinement')


def validate(root, assessment, inventory, *, scope='refinement'):
    accepted = positive_decision(scope)
    # Runtime validation is also used by direct Python callers and on recovery.
    concerns = MOTION_CONCERNS if scope == 'restrained-receptor' else CONCERNS
    if not isinstance(assessment, dict) or set(assessment) != set(concerns):
        raise ValueError('Environment review must address all four concerns')
    for concern, item in assessment.items():
        if (not isinstance(item, dict) or set(item) != {'decision', 'rationale', 'evidence', 'limitations'}
                or item['decision'] not in {accepted, 'requires_revision', 'evidence_insufficient'}
                or not text(item['rationale'], 20, 4000)):
            raise ValueError('Invalid environment judgment: ' + concern)
        limitations, citations = item['limitations'], item['evidence']
        if (not isinstance(limitations, list) or not 1 <= len(limitations) <= 16
                or any(not text(value, 5, 1000) for value in limitations)):
            raise ValueError('Environment judgment requires explicit limitations: ' + concern)
        if not isinstance(citations, list) or not 1 <= len(citations) <= 16:
            raise ValueError('Environment judgment requires bound evidence: ' + concern)
        for citation in citations:
            if (not isinstance(citation, dict) or set(citation) != {'path', 'sha256', 'locator', 'observation'}
                    or not isinstance(citation['path'], str)
                    or not isinstance(citation['sha256'], str)
                    or citation['path'] not in inventory
                    or inventory[citation['path']] != citation['sha256']
                    or not text(citation['locator'], 1, 500)
                    or not text(citation['observation'], 10, 2000)):
                raise ValueError('Environment citation must reference current bound evidence: ' + concern)
            cited_value(root, citation)
    decisions = {item['decision'] for item in assessment.values()}
    if 'requires_revision' in decisions:
        return 'requires_revision'
    if decisions == {accepted}:
        return accepted
    return 'evidence_insufficient'


def build(root, bound_input, inventory, assessment, *, scope='refinement'):
    decision = validate(root, assessment, inventory, scope=scope)
    # Bind the complete inventory, including files not selected as citations.
    for name, checksum in inventory.items():
        regular(root, root / name, checksum)
    return {'schema': 'receptor-environment-review-v1', 'bound_input': bound_input,
        **({'review_scope': scope} if scope != 'refinement' else {}),
        'lineage_sha256': inventory, 'assessment': assessment, 'decision': decision,
        'cited_values': {name: [{'path': citation['path'], 'sha256': citation['sha256'],
            'locator': citation['locator'], 'value': cited_value(root, citation)}
            for citation in item['evidence']] for name, item in assessment.items()},
        'reviewer_kind': 'agent_assessment', 'measurement_or_experimental_validation': False,
        'citation_entailment_verified': False, 'biological_suitability_verified': False,
        'reactivity_validated': False, 'execution_authorized': False, 'md_ready': False,
        'interpretation': 'A source-bound agent judgment for this exploratory ' + scope + ' only; '
            'provenance checks do not independently validate the reasoning or establish inhibitor activity.',
        'implementation_sha256': sha256_file(Path(__file__))}


def reference(root, path, payload):
    checksum = sha256_file(path)
    return {'artifact_id': 'environment-review-' + checksum[:32], 'manifest_sha256': checksum,
        'path': str(path.relative_to(root)), 'decision': payload['decision'],
        'reviewer_kind': 'agent_assessment', 'execution_authorized': False,
        'biological_suitability_verified': False, 'md_ready': False}


def freeze(root, bound_input, inventory, assessment, *, scope='refinement'):
    payload = build(root, bound_input, inventory, assessment, scope=scope)
    raw = encoded(payload)
    checksum = hashlib.sha256(raw).hexdigest()
    folder = root / 'state/environment-reviews' / checksum[:32]
    if any(path.is_symlink() for path in (root / 'state', folder.parent, folder)):
        raise ValueError('Environment review cannot traverse symbolic links')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'review.json'
    if not path.exists():
        ArtifactRegistry._write_json(path, json.loads(raw))
    regular(root, path, checksum)
    # Do not commit a review if cited or uncited parents changed while saving.
    if payload != build(root, bound_input, inventory, assessment, scope=scope):
        raise ValueError('Environment evidence changed during review')
    return reference(root, path, payload)


def load(root, ref, bound_input, inventory, *, scope='refinement'):
    checksum = ref['manifest_sha256']
    expected = 'state/environment-reviews/' + checksum[:32] + '/review.json'
    if ref['path'] != expected:
        raise ValueError('Environment review path differs from its identity')
    path, _ = regular(root, root / expected, checksum)
    payload = json.loads(path.read_text())
    if (payload != build(root, bound_input, inventory, payload['assessment'], scope=scope)
            or ref != reference(root, path, payload)):
        raise ValueError('Environment review differs from current bound evidence')
    return payload
