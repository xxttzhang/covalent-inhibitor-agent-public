"""Map explicit canonical UniProt cysteine to author numbering, without guessing offsets."""
import re


def map_cysteine(entity, instance, accession, residue):
    ids = entity["rcsb_polymer_entity_container_identifiers"]
    chain = instance["rcsb_polymer_entity_instance_container_identifiers"]
    if any(ids.get(key) != chain.get(key) for key in ("entry_id", "entity_id")):
        raise ValueError("Entity and chain identity mismatch")
    if accession not in ids.get("uniprot_ids", []):
        raise ValueError("Requested UniProt accession is absent from entity")
    positions = set()
    for alignment in entity.get("rcsb_polymer_entity_align", []):
        if (alignment.get("reference_database_name") != "UniProt"
                or alignment.get("reference_database_accession") != accession):
            continue
        for region in alignment.get("aligned_regions", []):
            offset = residue - int(region["ref_beg_seq_id"])
            if 0 <= offset < int(region["length"]):
                positions.add(int(region["entity_beg_seq_id"]) + offset)
    if len(positions) != 1:
        raise ValueError("Cys mapping is missing or ambiguous")
    position = positions.pop()
    sequence = "".join(entity["entity_poly"]["pdbx_seq_one_letter_code_can"].split())
    if not 1 <= position <= len(sequence) or sequence[position - 1] != "C":
        raise ValueError("Mapped entity residue is not cysteine")
    mapping = chain.get("auth_to_entity_poly_seq_mapping", [])
    if len(mapping) != len(sequence) or not mapping[position - 1]:
        raise ValueError("Author numbering map is incomplete")
    author = str(mapping[position - 1])
    if not re.fullmatch(r"-?\d+[A-Za-z]?", author):
        raise ValueError("Unsupported author residue identifier")
    return {"status": "sequence_mapping_verified_coordinates_pending",
        "entry_id": ids["entry_id"], "entity_id": ids["entity_id"],
        "label_asym_id": chain["asym_id"], "auth_asym_id": chain["auth_asym_id"],
        "uniprot": accession, "uniprot_residue": residue,
        "entity_seq_id": position, "author_residue_id": author, "residue": "CYS",
        "remaining_checks": ["SG coordinate present", "alternate locations and occupancy",
                             "ligand identity and noncovalent linkage", "binding-site proximity"]}
