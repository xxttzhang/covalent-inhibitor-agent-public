"""Compare a deposited construct with a verified canonical target sequence.

Alignment evidence describes sequence correspondence, not resolved coordinates.
This audit never approves a construct or fills missing residues.
"""
import hashlib
import re
import json


def construct_evidence_digest(evidence):
    bound = {key: evidence[key] for key in ('construct_audit', 'source_files', 'source_site_mapping')}
    return hashlib.sha256(json.dumps(bound, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def validate_construct_review(evidence, review):
    """Validate a source-bound judgment, not scientific truth or compute permission.

    The only positive scope retains the entire deposited construct unchanged.
    Internal gaps, nonstandard residues and sequence/species discrepancies
    cannot be admitted by this terminal-segment review contract.
    """
    required = {'evidence_sha256', 'decision', 'rationale', 'limitations', 'segments'}
    if not isinstance(review, dict) or set(review) != required:
        raise ValueError('Construct review requires exact evidence, decision, rationale, limitations and segments')
    if review['evidence_sha256'] != construct_evidence_digest(evidence):
        raise ValueError('Construct review evidence changed')
    if review['decision'] not in {'retain_deposited_construct', 'requires_revision', 'evidence_insufficient'}:
        raise ValueError('Unsupported construct review decision')
    if (not isinstance(review['rationale'], str) or not 20 <= len(review['rationale'].strip()) <= 4000
            or not isinstance(review['limitations'], list) or not 1 <= len(review['limitations']) <= 16
            or any(not isinstance(item, str) or not 5 <= len(item.strip()) <= 1000
                   for item in review['limitations'])):
        raise ValueError('Construct review needs a substantive rationale and explicit limitations')
    audit = evidence['construct_audit']
    segments = audit['unaligned_segments']
    assessments = review['segments']
    if not isinstance(assessments, list) or len(assessments) != len(segments):
        raise ValueError('Review every unaligned segment exactly once')
    expected = {f'/construct_audit/unaligned_segments/{index}' for index in range(len(segments))}
    seen = set()
    for assessment in assessments:
        if (not isinstance(assessment, dict) or set(assessment) != {'locator', 'observation', 'rationale'}
                or not isinstance(assessment['locator'], str) or assessment['locator'] not in expected
                or assessment['locator'] in seen
                or any(not isinstance(assessment[key], str)
                       or not 20 <= len(assessment[key].strip()) <= 2000 for key in ('observation', 'rationale'))):
            raise ValueError('Each segment needs its exact evidence locator, observation and judgment rationale')
        seen.add(assessment['locator'])
    if review['decision'] == 'retain_deposited_construct':
        if (not audit['source_species_verified'] or audit['sequence_differences'] or audit['uncertain_residues']
                or not segments or any(row['location_relative_to_alignment'] not in {'N_terminal', 'C_terminal'}
                    or row['nonstandard_entity_positions'] for row in segments)):
            raise ValueError('This review cannot admit internal, nonstandard, variant or unverified construct scope')
    return {'schema': 'ligand-construct-review-v1', 'review': review,
            'evidence_sha256': review['evidence_sha256'],
            'scope': 'retain entire deposited construct; no clipping, renumbering or sequence correction',
            'scientific_judgment_verified': False, 'coordinates_modified': False,
            'grants_resource_approval': False, 'receptor_prepared': False, 'md_ready': False}


def audit_construct(entity, canonical_sequence, accession, taxon, *, include_segment_evidence=False):
    if not isinstance(canonical_sequence, str) or not re.fullmatch('[A-Z]+', canonical_sequence):
        raise ValueError('Canonical sequence must be explicit uppercase amino-acid codes')
    if type(taxon) is not int or taxon < 1:
        raise ValueError('An explicit positive target taxon is required')
    ids = entity['rcsb_polymer_entity_container_identifiers']
    if accession not in ids.get('uniprot_ids', []):
        raise ValueError('Construct does not reference the verified target')
    sequence = ''.join(entity['entity_poly']['pdbx_seq_one_letter_code_can'].split())
    if not re.fullmatch('[A-Z]+', sequence):
        raise ValueError('Unsupported construct sequence')
    forward, reverse = {}, {}
    for alignment in entity.get('rcsb_polymer_entity_align', []):
        if (alignment.get('reference_database_name') != 'UniProt'
                or alignment.get('reference_database_accession') != accession):
            continue
        for region in alignment.get('aligned_regions', []):
            start, ref, length = (region[k] for k in
                ('entity_beg_seq_id', 'ref_beg_seq_id', 'length'))
            if any(type(n) is not int or n < 1 for n in (start, ref, length)):
                raise ValueError('Alignment coordinates must be positive integers')
            if start + length - 1 > len(sequence) or ref + length - 1 > len(canonical_sequence):
                raise ValueError('Alignment extends beyond a sequence')
            for offset in range(length):
                a, b = start + offset, ref + offset
                if (a in forward and forward[a] != b) or (b in reverse and reverse[b] != a):
                    raise ValueError('Ambiguous construct alignment')
                forward[a], reverse[b] = b, a
    if not forward:
        raise ValueError('No construct alignment to the verified target')
    if list(sorted(reverse)) != [forward[a] for a in sorted(forward)]:
        raise ValueError('Non-monotonic construct alignment')
    changes, uncertain = [], []
    standard = set('ACDEFGHIKLMNPQRSTVWY')
    for position, reference in sorted(forward.items()):
        observed, expected = sequence[position - 1], canonical_sequence[reference - 1]
        row = {'entity_position': position, 'canonical_position': reference,
               'construct_residue': observed, 'canonical_residue': expected}
        if observed not in standard or expected not in standard:
            uncertain.append(row)
        elif observed != expected:
            changes.append(row)
    organisms = entity.get('rcsb_entity_source_organism', [])
    source_taxa = sorted({o['ncbi_taxonomy_id'] for o in organisms
                          if type(o.get('ncbi_taxonomy_id')) is int})
    species_verified = bool(organisms) and all(o.get('ncbi_taxonomy_id') == taxon
        and type(o.get('ncbi_taxonomy_id')) is int for o in organisms)
    unaligned = sorted(set(range(1, len(sequence) + 1)) - forward.keys())
    segments = []
    for position in unaligned:
        if not segments or position != segments[-1][-1] + 1:
            segments.append([])
        segments[-1].append(position)
    segment_evidence = []
    for positions in segments:
        start, end = positions[0], positions[-1]
        location = ('N_terminal' if end < min(forward) else
                    'C_terminal' if start > max(forward) else 'internal')
        segment_evidence.append({'entity_start': start, 'entity_end': end,
            'entity_positions': positions, 'sequence': sequence[start - 1:end],
            'location_relative_to_alignment': location,
            'nonstandard_entity_positions': [p for p in positions if sequence[p - 1] not in standard],
            'origin': 'not_assigned', 'coordinate_presence': 'not_assessed',
            'functional_effect': 'not_assessed'})
    return {'schema': 'target-construct-audit-v1', 'status': 'construct_review_required',
            'entry_id': ids['entry_id'], 'entity_id': ids['entity_id'], 'uniprot': accession,
            'canonical_sequence_sha256': hashlib.sha256(canonical_sequence.encode()).hexdigest(),
            'construct_sequence_sha256': hashlib.sha256(sequence.encode()).hexdigest(),
            'canonical_length': len(canonical_sequence), 'construct_length': len(sequence),
            'aligned_positions': len(forward), 'sequence_differences': changes,
            'uncertain_residues': uncertain,
            'unaligned_entity_positions': unaligned,
            **({'unaligned_segments': segment_evidence} if include_segment_evidence else {}),
            'canonical_positions_absent_from_alignment': sorted(set(range(1, len(canonical_sequence) + 1)) - reverse.keys()),
            'source_taxa': source_taxa, 'expected_taxon': taxon,
            'source_species_verified': species_verified,
            'receptor_prepared': False, 'campaign_ready': False,
            'interpretation': 'Unaligned residues require construct review; absent canonical positions are not automatically missing loops. Coordinate completeness and chemistry require separate checks.'}
