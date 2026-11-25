import hashlib
import json
from pathlib import Path

import pytest

from covscreen.mcp.server import dispatch
from covscreen.target_identity import parse_targets, target_plan


@pytest.mark.parametrize('count', [None, 60, 61, True])
def test_sealed_identity_count_compatibility_and_validation(tmp_path, count):
    from covscreen.target_identity import load_target_identity
    import shutil
    folder = Path(__file__).parent / 'fixtures/target_identity'
    for src, dst in [('egfr.json', 'uniprot.json'), ('P00533.fasta', 'P00533.fasta'),
                     ('egfr-manifest.json', 'target_identity.json')]:
        shutil.copyfile(folder / src, tmp_path / dst)
    path = tmp_path / 'target_identity.json'
    manifest = json.loads(path.read_text())
    candidate = manifest['identity']['candidates'][0]
    assert len(candidate['canonical_cysteines']) == 60
    if count is not None:
        candidate['canonical_cysteine_count'] = count
    path.write_text(json.dumps(manifest))
    before = path.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    if count is None or type(count) is int and count == 60:
        assert load_target_identity(tmp_path, digest) == manifest
    else:
        with pytest.raises(ValueError):
            load_target_identity(tmp_path, digest)
    assert path.read_bytes() == before


def entry(accession='P00533', gene='EGFR', taxon=9606, sequence='MACC'):
    return {'primaryAccession': accession, 'genes': [{'geneName': {'value': gene}}],
            'organism': {'taxonId': taxon}, 'entryType': 'UniProtKB reviewed (Swiss-Prot)',
            'sequence': {'value': sequence}}


def test_resolves_identity_without_ligand_or_preselected_site():
    result = parse_targets({'results': [entry()]}, 'EGFR', 9606, 1)
    assert result['unique_verified_target'] == 'P00533'
    target = result['candidates'][0]
    assert target['canonical_cysteines'] == [3, 4]
    assert target['canonical_cysteine_count'] == 2
    assert target['sequence_sha256'] == hashlib.sha256(b'MACC').hexdigest()
    assert 'selected_site' not in target


def test_target_without_cysteine_is_an_identity_not_a_screening_hit():
    result = parse_targets({'results': [entry(sequence='MAAA')]}, 'EGFR', 9606, 1)
    assert result['unique_verified_target'] == 'P00533'
    assert result['candidates'][0]['canonical_cysteines'] == []
    assert result['candidates'][0]['canonical_cysteine_count'] == 0


@pytest.mark.parametrize('total', [None, 2])
def test_incomplete_page_never_resolves_identity(total):
    result = parse_targets({'results': [entry()]}, 'EGFR', 9606, total)
    assert result['unique_verified_target'] is None
    assert not result['complete_search']


def test_ambiguous_primary_gene_never_picks_first_hit():
    result = parse_targets({'results': [entry(), entry(accession='P00534')]}, 'EGFR', 9606, 2)
    assert result['unique_verified_target'] is None


@pytest.mark.parametrize('row', [entry(gene='OTHER'), entry(taxon=10090),
    {**entry(), 'entryType': 'UniProtKB unreviewed (TrEMBL)'}])
def test_synonym_species_and_unreviewed_fallbacks_are_rejected(row):
    result = parse_targets({'results': [row]}, 'EGFR', 9606, 1)
    assert result['unique_verified_target'] is None
    assert result['local_filtered'] == 1


@pytest.mark.parametrize('gene,taxon', [('EGFR OR *', 9606), ('EGFR', True), ('EGFR', 0), ('', 9606)])
def test_invalid_target_queries_are_rejected(gene, taxon):
    with pytest.raises(ValueError):
        target_plan(gene, taxon)


def test_duplicate_accessions_and_invalid_counts_are_errors():
    with pytest.raises(ValueError, match='duplicate'):
        parse_targets({'results': [entry(), entry()]}, 'EGFR', 9606, 2)
    for total in (0, True, -1):
        with pytest.raises(ValueError, match='count'):
            parse_targets({'results': [entry()]}, 'EGFR', 9606, total)


def test_mcp_plan_does_not_create_artifacts_or_need_a_ligand(tmp_path):
    result = dispatch(tmp_path, 'covalent_library_first_resolve_target',
                      {'gene': 'BTK', 'taxon': 9606})
    assert result['status'] == 'PASS'
    assert not result['network_request_sent']
    assert not result['calculation_submitted']
    assert result['plan']['max_calls'] == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('gene,accession,site', [('EGFR', 'P00533', 797), ('BTK', 'Q06187', 481)])
def test_downloaded_public_identity_evidence_is_replayed_without_network(gene, accession, site):
    folder = Path(__file__).parent / 'fixtures/target_identity'
    raw = (folder / (gene.lower() + '.json')).read_bytes()
    source = json.loads((folder / (gene.lower() + '-source.json')).read_text())
    assert hashlib.sha256(raw).hexdigest() == source['source']['sha256']
    result = parse_targets(json.loads(raw), gene, 9606, source['server_total'])
    assert result['unique_verified_target'] == accession
    assert site in result['candidates'][0]['canonical_cysteines']
    assert result['candidates'][0]['canonical_cysteine_count'] == len(result['candidates'][0]['canonical_cysteines'])
    # This is a sequence identity assertion, never proof of site reactivity.
