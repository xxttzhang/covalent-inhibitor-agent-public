"""Identity validation plus an install-time hook for evidence retrieval."""
import argparse
import json
import re
from pathlib import Path


class IdentityError(ValueError):
    """An input/identity mismatch that an unchanged retry cannot resolve."""


class QueryRejected(ValueError):
    """A permanent HTTP request rejection, not a transient network outage."""


def resolve_identity(request_get, target, site, taxon):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,39}', target):
        raise IdentityError('Use an exact gene symbol or canonical UniProt accession')
    # UniProt validates accession syntax even inside an OR expression.
    # https://www.uniprot.org/help/accession_numbers
    accession_input = re.fullmatch(r'[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2}', target)
    field = 'accession' if accession_input else 'gene_exact'
    response = request_get("https://rest.uniprot.org/uniprotkb/search", timeout=45,
        params={"query": f"({field}:{target}) AND organism_id:{taxon} AND reviewed:true",
            "format": "json", "size": 10, "fields": "accession,gene_names,organism_id,reviewed,sequence"})
    if getattr(response, 'status_code', None) in {400, 401, 403, 404, 422}:
        raise QueryRejected(f'UniProt identity query rejected with HTTP {response.status_code}; '
                            'check the query adapter or service access; do not retry unchanged inputs')
    response.raise_for_status()
    matches = response.json().get("results", [])
    total = response.headers.get("x-total-results")
    if len(matches) != 1 or (total is not None and str(total) != "1"):
        raise IdentityError("Target must resolve to exactly one reviewed gene/accession in the requested species")
    entry = matches[0]
    genes = [g.get("geneName", {}).get("value") for g in entry.get("genes", [])]
    accession = entry.get("primaryAccession")
    if (entry.get("organism", {}).get("taxonId") != taxon
        or entry.get("entryType") != "UniProtKB reviewed (Swiss-Prot)"
        or (target != accession and target not in genes)
        or not genes or not isinstance(genes[0], str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,39}", genes[0])):
        raise IdentityError("Returned identity does not match the exact target, species and reviewed primary gene")
    sequence = entry.get("sequence", {}).get("value", "")
    if not re.fullmatch(r"[A-Z]+", sequence) or not re.fullmatch(r"C[1-9][0-9]{0,4}", site):
        raise IdentityError("Canonical sequence or cysteine site is invalid")
    position = int(site[1:])
    if position > len(sequence) or sequence[position - 1] != "C":
        raise IdentityError("Requested site is not cysteine in the canonical sequence")
    return {"uniprot_id": accession, "gene": genes[0], "requested_target": target,
        "sequence_length": len(sequence), "taxon": taxon,
        "canonical_cys_verified": True, "sequence": sequence}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--site", required=True)
    parser.add_argument("--taxon", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "retrieval_failure.json").write_text(json.dumps({
        "reason": "retrieval_adapter_not_distributed", "retryable": False,
        "detail": "The public source distribution excludes the unlicensed retrieval adapter.",
        "next_action": "Install a separately licensed retrieval adapter and bind it explicitly before collecting evidence."
    }, ensure_ascii=False, indent=2))
    raise SystemExit("Evidence retrieval adapter is not distributed in the public source package")


if __name__ == "__main__":
    main()
