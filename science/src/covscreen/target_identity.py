"""Bounded target identity and canonical sequence evidence shared across routes.

Cysteine positions are sequence observations, not reactive-site predictions.
No receptor, campaign, or computation is approved by this module.
"""
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def target_plan(gene, taxon):
    if not isinstance(gene, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,39}', gene):
        raise ValueError('An explicit primary gene symbol is required')
    if type(taxon) is not int or taxon < 1:
        raise ValueError('An explicit positive taxon ID is required')
    params = {'query': f'(gene_exact:{gene}) AND (organism_id:{taxon}) AND (reviewed:true)',
              'format': 'json', 'size': 20,
              'fields': 'accession,gene_names,organism_id,organism_name,reviewed,sequence'}
    return {'gene': gene, 'taxon': taxon,
            'uniprot_url': 'https://rest.uniprot.org/uniprotkb/search?' + urllib.parse.urlencode(params),
            'max_calls': 1, 'max_response_bytes': 2_000_000, 'requires_compute': False,
            'policy': 'Reviewed primary symbol and explicit species; no synonym or species fallback.'}


def parse_targets(payload, gene, taxon, total):
    target_plan(gene, taxon)
    rows = payload.get('results') if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 20 or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Invalid/beyond-limit UniProt results')
    if total is not None and (type(total) is not int or total < len(rows)):
        raise ValueError('UniProt count mismatch')
    hits, seen = [], set()
    for row in rows:
        accession = row.get('primaryAccession', '')
        if not isinstance(accession, str) or not re.fullmatch(r'[A-Z0-9]{6,10}', accession) or accession in seen:
            raise ValueError('Invalid/duplicate UniProt accession')
        seen.add(accession)
        genes = [g.get('geneName', {}).get('value') for g in row.get('genes', [])]
        if (gene not in genes or row.get('organism', {}).get('taxonId') != taxon
                or row.get('entryType') != 'UniProtKB reviewed (Swiss-Prot)'):
            continue
        sequence = row.get('sequence', {}).get('value', '')
        if not isinstance(sequence, str) or not re.fullmatch('[A-Z]{1,100000}', sequence):
            raise ValueError('Missing/invalid canonical sequence')
        hits.append({'uniprot_id': accession, 'gene': gene, 'taxon': taxon,
                     'sequence': sequence, 'sequence_length': len(sequence),
                     'sequence_sha256': hashlib.sha256(sequence.encode()).hexdigest(),
                     'canonical_cysteine_count': sequence.count('C'),
                     'canonical_cysteines': [i for i, residue in enumerate(sequence, 1) if residue == 'C']})
    complete = total == len(rows)
    return {'server_total': total, 'retrieved': len(rows), 'local_filtered': len(rows) - len(hits),
            'complete_search': complete, 'candidates': hits,
            'unique_verified_target': hits[0]['uniprot_id'] if complete and len(hits) == 1 else None}


def resolve_target(gene, taxon, out):
    plan = target_plan(gene, taxon)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    result = {'schema': 'target-identity-v1', 'status': 'lookup_started', 'plan': plan,
              'accessed_at': datetime.now(timezone.utc).isoformat(),
              'sources': [], 'campaign_ready': False, 'calculation_submitted': False}
    try:
        request = urllib.request.Request(plan['uniprot_url'], headers={'Accept': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(plan['max_response_bytes'] + 1)
            count = response.headers.get('x-total-results')
            release = response.headers.get('x-uniprot-release')
            release_date = response.headers.get('x-uniprot-release-date')
            next_page = response.headers.get('link')
        if len(raw) > plan['max_response_bytes']:
            raise ValueError('UniProt response exceeds bounded payload limit')
        (out / 'uniprot.json').write_bytes(raw)
        result['sources'].append({'url': plan['uniprot_url'], 'path': 'uniprot.json',
                                  'sha256': hashlib.sha256(raw).hexdigest(),
                                  'release': release, 'release_date': release_date,
                                  'total_results_header': count, 'link_header': next_page})
        total = int(count) if count is not None else None
        identity = parse_targets(json.loads(raw), gene, taxon, total)
        if next_page and 'rel="next"' in next_page and identity['complete_search']:
            raise ValueError('UniProt pagination contradicts total count')
        for candidate in identity['candidates']:
            sequence = candidate.pop('sequence')
            name = candidate['uniprot_id'] + '.fasta'
            fasta = f">{candidate['uniprot_id']} {gene} taxon={taxon} canonical\n{sequence}\n"
            (out / name).write_text(fasta)
            candidate.update(sequence_path=name, fasta_sha256=hashlib.sha256(fasta.encode()).hexdigest())
        result['identity'] = identity
        result['status'] = ('identity_resolved_site_assessment_required' if identity['unique_verified_target']
                            else 'identity_incomplete_or_ambiguous')
        result['site_assessment'] = {'selected_site': None, 'reactivity_validated': False,
            'required_evidence': ['structure and canonical-to-author residue mapping',
                                  'site accessibility and local chemical environment',
                                  'supported reaction chemistry and receptor preparation']}
    except (ValueError, KeyError, TypeError, AttributeError, urllib.error.URLError, TimeoutError) as exc:
        result.update(status='lookup_failed', error=str(exc))
    (out / 'target_identity.json').write_text(json.dumps(result, indent=2, allow_nan=False) + '\n')
    return result


def load_target_identity(directory, manifest_sha256):
    """Recompute identity from saved source bytes before another tool consumes it."""
    directory = Path(directory)
    if not isinstance(manifest_sha256, str) or not re.fullmatch('[a-f0-9]{64}', manifest_sha256):
        raise ValueError('Explicit target manifest SHA256 required')
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Target artifact must be a regular directory')

    def read(name):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Target artifact file is missing or not regular')
        return path.read_bytes()

    raw = read('target_identity.json')
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ValueError('Target manifest checksum differs')
    result = json.loads(raw)
    if (result.get('schema') != 'target-identity-v1'
            or result.get('status') != 'identity_resolved_site_assessment_required'
            or result.get('campaign_ready') is not False
            or result.get('calculation_submitted') is not False):
        raise ValueError('A resolved, nonexecuting target identity artifact is required')
    plan = result['plan']
    if plan != target_plan(plan['gene'], plan['taxon']):
        raise ValueError('Target plan differs from its declared identity')
    sources = result.get('sources')
    if not isinstance(sources, list) or len(sources) != 1:
        raise ValueError('One complete UniProt source is required')
    source = sources[0]
    if source.get('path') != 'uniprot.json' or source.get('url') != plan['uniprot_url']:
        raise ValueError('Target source address differs from plan')
    payload = read('uniprot.json')
    if len(payload) > plan['max_response_bytes'] or hashlib.sha256(payload).hexdigest() != source.get('sha256'):
        raise ValueError('UniProt source checksum or size differs')
    count = source.get('total_results_header')
    if not isinstance(count, str) or not re.fullmatch('[0-9]+', count):
        raise ValueError('Complete target count evidence is required')
    if 'rel="next"' in (source.get('link_header') or ''):
        raise ValueError('Target source has an unconsumed next page')
    identity = parse_targets(json.loads(payload), plan['gene'], plan['taxon'], int(count))
    if not identity['unique_verified_target']:
        raise ValueError('Saved source does not resolve a unique target')
    for candidate in identity['candidates']:
        sequence = candidate.pop('sequence')
        name = candidate['uniprot_id'] + '.fasta'
        expected = f">{candidate['uniprot_id']} {plan['gene']} taxon={plan['taxon']} canonical\n{sequence}\n".encode()
        fasta = read(name)
        if fasta != expected:
            raise ValueError('FASTA differs from verified source sequence')
        candidate.update(sequence_path=name, fasta_sha256=hashlib.sha256(fasta).hexdigest())
    # Historical v1 artifacts predate the derived count; retain their exact bytes.
    saved_candidates = result.get('identity', {}).get('candidates', [])
    for candidate, saved in zip(identity['candidates'], saved_candidates):
        if 'canonical_cysteine_count' not in saved:
            candidate.pop('canonical_cysteine_count')
        elif type(saved['canonical_cysteine_count']) is not int:
            raise ValueError('Saved cysteine count must be an integer')
    if identity != result.get('identity'):
        raise ValueError('Saved identity differs from raw UniProt evidence')
    assessment = result.get('site_assessment', {})
    if assessment.get('selected_site') is not None or assessment.get('reactivity_validated') is not False:
        raise ValueError('Identity artifact cannot certify a reaction site')
    return result
