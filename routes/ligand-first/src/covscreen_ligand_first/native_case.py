"""Prepare a native enumeration case from frozen acquisition evidence.

This is input preparation, not automatic approval of downstream runtime settings.
"""
import hashlib
import json
import re
import shutil
from pathlib import Path

from .ligand_first_inputs import LigandFirstCampaignSpec, audit_ligand_first_inputs, write_parent_pose_sdf
from covscreen.parent_template import build_parent_template
from covscreen.rcsb_mapping import map_cysteine


def prepare_native_case(acquisition_dir: Path, out: Path, *, campaign_id: str,
                        gene: str, taxon: int, ligand_name: str,
                        noncovalent_parent_assertion: str, identity_record: Path | None = None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,100}", campaign_id):
        raise ValueError("Invalid campaign ID")
    if not gene.strip() or not ligand_name.strip() or not noncovalent_parent_assertion.strip():
        raise ValueError("Explicit target/ligand names and noncovalent-parent assertion required")
    if type(taxon) is not int or taxon < 1:
        raise ValueError("Explicit taxon ID required")
    source = acquisition_dir.resolve()
    acquisition = json.loads((source / "structure_acquisition.json").read_text())
    if acquisition.get("status") != "acquired_coordinate_audit_completed_review_pending":
        raise ValueError("Completed acquisition audit required")
    frozen = {}
    for asset in acquisition["sources"]:
        path = (source / asset["path"]).resolve()
        if not path.is_relative_to(source) or path in frozen:
            raise ValueError("Invalid or duplicate acquisition source path")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != asset["sha256"]:
            raise ValueError("Acquisition source hash mismatch")
        frozen[path] = asset
    if len(acquisition.get("mappings", [])) != 1:
        raise ValueError("Unique Cys mapping required")
    mapping = acquisition["mappings"][0]
    entity_id = mapping["entity_id"]
    if not re.fullmatch(r"[0-9]+", entity_id):
        raise ValueError("Invalid entity ID")
    needed = [source / name for name in ("entry.json", f"entity_{entity_id}.json", "structure.pdb", "component.cif")]
    if any(path.resolve() not in frozen for path in needed):
        raise ValueError("Required acquisition evidence is not frozen")
    entry, entity = [json.loads(path.read_text()) for path in needed[:2]]
    chain_id = mapping["label_asym_id"]
    if not isinstance(chain_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,8}", chain_id):
        raise ValueError("Invalid label chain")
    chain_path = source / f"chain_{chain_id}.json"
    if chain_path not in frozen:
        raise ValueError("Frozen chain mapping source required")
    verified_mapping = map_cysteine(entity, json.loads(chain_path.read_text()),
        acquisition["plan"]["uniprot"], acquisition["plan"]["cys"])
    if mapping != verified_mapping or entry["rcsb_entry_container_identifiers"]["entry_id"] != mapping["entry_id"]:
        raise ValueError("Saved Cys mapping differs from frozen source metadata")
    organisms = entity.get("rcsb_entity_source_organism", [])
    taxa = {row.get("ncbi_taxonomy_id") for row in organisms}
    if taxa != {taxon}:
        raise ValueError("Source organism mismatch or ambiguity")
    declared_genes = {g.get("value", "").casefold() for row in organisms
                      for g in row.get("rcsb_gene_name", [])}
    if gene.casefold() not in declared_genes:
        if identity_record is None:
            raise ValueError("Requested gene label is not verified by source metadata")
        identity = json.loads(identity_record.read_text())
        names = {item.get("geneName", {}).get("value", "").casefold() for item in identity.get("genes", [])}
        if (identity.get("primaryAccession") != mapping["uniprot"]
            or identity.get("organism", {}).get("taxonId") != taxon or gene.casefold() not in names):
            raise ValueError("UniProt gene/accession/species identity mismatch")
    organism_names = {row.get("ncbi_scientific_name") for row in organisms}
    if len(organism_names) != 1 or not all(organism_names):
        raise ValueError("Source organism name missing/ambiguous")
    # Nonzero or unknown differences require a mutation-aware case, never a silent WT claim.
    sequence_meta = entity.get("entity_poly", {})
    for key in ("rcsb_mutation_count", "rcsb_conflict_count", "rcsb_insertion_count", "rcsb_deletion_count"):
        if type(sequence_meta.get(key)) is not int or sequence_meta[key] != 0:
            raise ValueError("Sequence alteration requires explicit review: " + key)
    resolutions = entry.get("rcsb_entry_info", {}).get("resolution_combined", [])
    methods = {item.get("method") for item in entry.get("exptl", [])}
    if len(resolutions) != 1 or methods != {"X-RAY DIFFRACTION"}:
        raise ValueError("This preparation adapter requires one reported X-ray resolution")
    if not re.fullmatch(r"-?[0-9]+", mapping["author_residue_id"]):
        raise ValueError("Campaign format cannot yet encode residue insertion codes")
    audit = acquisition["coordinate_audit"]
    if audit.get("explicit_connections"):
        raise ValueError("Explicit parent linkage requires review before noncovalent case preparation")
    component = acquisition["plan"]["component"]
    expected_key = acquisition["plan"].get("expected_inchikey")
    if not expected_key:
        raise ValueError("Expected chemical identity is missing")
    out.mkdir(parents=True, exist_ok=False)
    structure_dir = out / "source_structures"
    structure_dir.mkdir()
    source_assets = []
    for name in ("structure.pdb", "component.cif"):
        shutil.copyfile(source / name, structure_dir / name)
        asset = frozen[source / name]
        source_assets.append({"path": "source_structures/" + name,
                              "sha256": asset["sha256"], "source_url": asset["url"]})
    evidence_dir = out / "evidence"
    evidence_dir.mkdir()
    if identity_record is not None:
        shutil.copyfile(identity_record, evidence_dir / "uniprot.json")
        source_assets.append({"path": "evidence/uniprot.json",
            "sha256": hashlib.sha256(identity_record.read_bytes()).hexdigest(),
            "source_url": "https://rest.uniprot.org/uniprotkb/" + mapping["uniprot"] + ".json"})
    for path in (needed[0], needed[1], chain_path):
        shutil.copyfile(path, evidence_dir / path.name)
    shutil.copyfile(source / "structure_acquisition.json", evidence_dir / "structure_acquisition.json")
    template = build_parent_template(structure_dir / "component.cif", structure_dir / "structure.pdb",
        mapping, component, expected_key, out / "template", ligand_chain=audit["ligand_author_chain"],
        ligand_residue=audit["ligand_author_residue"], model=audit["model"], altloc=audit["altloc"])
    if template["coordinate_audit"]["explicit_connections"]:
        raise ValueError("Re-read coordinates show explicit parent linkage")
    source_assets.append({"path": "template/parent_template.sdf", "sha256": template["template_sha256"],
                          "source_url": "derived:ccd-sha256:" + template["chemical_identity"]["ccd_sha256"]})
    parent = {"pdb_id": mapping["entry_id"], "structure_path": "source_structures/structure.pdb",
        "ligand_component_id": component, "ligand_name": ligand_name,
        "ligand_chain_id": audit["ligand_author_chain"],
        "ligand_author_residue_number": int(audit["ligand_author_residue"]),
        "ligand_template_path": "template/parent_template.sdf", "protein_chain_id": mapping["auth_asym_id"],
        "target_author_residue_number": int(mapping["author_residue_id"]),
        "target_uniprot_residue_number": mapping["uniprot_residue"],
        "residue_number_offset": mapping["uniprot_residue"] - int(mapping["author_residue_id"]),
        "experimental_method": "X-RAY DIFFRACTION", "resolution_angstrom": resolutions[0],
        "pdb_doi": "10.2210/pdb" + mapping["entry_id"] + "/pdb",
        "primary_citation_doi": entry.get("rcsb_primary_citation", {}).get("pdbx_database_id_DOI", "")}
    campaign = LigandFirstCampaignSpec.model_validate({"schema_version": "1.0", "campaign_id": campaign_id,
        "planning_mode": "upstream-native", "target": {"symbol": gene, "uniprot_id": mapping["uniprot"],
            "organism": next(iter(organism_names)), "nucleophile": {"residue_name": "CYS",
                "uniprot_residue_number": mapping["uniprot_residue"], "atom_name": "SG"}},
        "parent_pose": parent, "ligand_atom_names_in_template_order": template["chemical_identity"]["coordinate_atom_order"],
        "allowed_warheads": ["acrylamide", "chloroacetamide", "propiolamide", "vinyl_sulfonamide"],
        "source_assets": source_assets})
    validated, molecule = audit_ligand_first_inputs(campaign, out)
    if validated.status != "PASS" or molecule is None:
        raise ValueError("Prepared case failed existing input audit: " + str(validated.errors))
    (out / "campaign.json").write_text(campaign.model_dump_json(indent=2))
    write_parent_pose_sdf(molecule, out / "parent_pose.sdf")
    (out / "input_audit.json").write_text(validated.model_dump_json(indent=2))
    result = {"status": "native_inputs_prepared_not_runtime_registered", "campaign_id": campaign_id,
        "campaign_path": str(out / "campaign.json"), "campaign_sha256": hashlib.sha256((out / "campaign.json").read_bytes()).hexdigest(),
        "noncovalent_parent_evidence": {"kind": "caller_assertion_not_independently_verified",
                                      "statement": noncovalent_parent_assertion},
        "acquisition_manifest_sha256": hashlib.sha256((source / "structure_acquisition.json").read_bytes()).hexdigest(),
        "sources": acquisition["sources"], "enumeration_backend": "upstream-native",
        "evidence_directory": "evidence",
        "runtime_registered": False, "calculation_submitted": False,
        "remaining": ["Bind approved engine/runtime settings", "Run parent-linked downstream stages",
                      "Independent experimental evidence is not supplied by input preparation"]}
    (out / "native_case_preparation.json").write_text(json.dumps(result, indent=2))
    return result
