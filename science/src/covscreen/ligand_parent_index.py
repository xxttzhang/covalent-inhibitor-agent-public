"""Provenance-bound candidate retrieval, never structural or activity admission."""
import csv
import hashlib
import io
import json
import re
from pathlib import Path


DATA = Path(__file__).with_name('data') / 'covpdb_noncovalent_near_cys.tsv'
REQUIRED = {'pdb_ligand_id', 'pdb_id', 'het_code', 'uniprot_id', 'ligand_id',
            'canonical_smiles', 'residue_name', 'residue_number', 'chain_id'}


def search(uniprot_id, *, offset=0, limit=20, dataset=DATA):
    if not isinstance(uniprot_id, str) or not re.fullmatch(r'[A-Z0-9]{6,10}', uniprot_id):
        raise ValueError('An explicit UniProt accession is required')
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Offset must be nonnegative; page size must be 1–50')
    dataset = Path(dataset)
    if not dataset.is_file() or not dataset.with_suffix('.manifest.json').is_file():
        raise ValueError('The public source distribution does not bundle a candidate index; pass a licensed dataset and matching manifest')
    raw = dataset.read_bytes()
    manifest = json.loads(dataset.with_suffix('.manifest.json').read_text())
    digest = hashlib.sha256(raw).hexdigest()
    if manifest.get('schema') != 'covpdb-parent-index-v1' or manifest.get('sha256') != digest:
        raise ValueError('Candidate index does not match its frozen manifest')
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')), delimiter='\t')
    if not REQUIRED.issubset(reader.fieldnames or []):
        raise ValueError('Candidate index lacks required columns')
    matches, seen = [], set()
    for row_number, row in enumerate(reader, 2):
        if None in row or any(row.get(key) is None for key in REQUIRED):
            raise ValueError('Malformed candidate index row')
        key = row['pdb_ligand_id']
        if not key or key in seen:
            raise ValueError('Missing or duplicate source row ID')
        seen.add(key)
        if row['uniprot_id'] != uniprot_id:
            continue
        if row['residue_name'] != 'CYS':
            raise ValueError('This index supports cysteine candidates only')
        matches.append({'source_row_id': key, 'source_line': row_number,
            'source_sha256': digest, 'record': row,
            'status': 'STRUCTURE_VERIFICATION_REQUIRED',
            'canonical_site': None, 'species_verified': False,
            'inhibitor_verified': False, 'execution_ready': False})
    return {'schema': 'ligand-parent-index-search-v1', 'uniprot_id': uniprot_id,
        'source': manifest, 'total_matches': len(matches), 'offset': offset,
        'candidates': matches[offset:offset + limit],
        'next_offset': offset + limit if offset + limit < len(matches) else None,
        'compute_submitted': False,
        'limitations': ['Rows are candidate observations, not unique validated biological sites.',
            'Recorded chain/residue must be mapped against the original structure and canonical sequence.',
            'Missing ABPP evidence and prediction scores are not negative biological results.',
            'No match does not establish absence of a suitable parent; use supplementary structure sources.']}
