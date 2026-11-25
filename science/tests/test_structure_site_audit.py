from pathlib import Path
import json
import os

import pytest

from covscreen.structure_site_audit import audit_structure_site

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get("COVSCREEN_TEST_1M17_PDB", str(ROOT / "projects/egfr-c797/ligand_first/source_structures/1M17.pdb")))


def mapping():
    return {"status": "sequence_mapping_verified_coordinates_pending", "entry_id": "1M17",
            "uniprot": "P00533", "uniprot_residue": 797,
            "auth_asym_id": "A", "author_residue_id": "773"}


def test_real_frozen_parent_structure():
    result = audit_structure_site(SOURCE, mapping(), "AQ4")
    assert result["ligand_author_residue"] == "999"
    assert result["ligand_heavy_atom_count"] == 29
    assert result["closest_ligand_atom"] == "C12"
    assert result["sg_ligand_min_distance_angstrom"] == pytest.approx(5.922939, abs=1e-6)
    assert result["minimum_occupancy"] == 1
    assert result["explicit_connections"] == []
    assert result["campaign_ready"] is False
    assert result["status"] == "coordinate_checks_passed_linkage_review_pending"


@pytest.mark.parametrize("field,value", [("entry_id", "4HJO"), ("auth_asym_id", "Z"),
    ("author_residue_id", "797"), ("author_residue_id", "773A"), ("status", "unknown")])
def test_wrong_mapping_is_rejected(field, value):
    changed = mapping()
    changed[field] = value
    with pytest.raises(ValueError):
        audit_structure_site(SOURCE, changed, "AQ4")


def mutate_file(tmp_path, transform):
    path = tmp_path / "1M17.pdb"
    path.write_text("\n".join(transform(SOURCE.read_text().splitlines())) + "\n")
    return path


def test_insertion_code_not_conflated_with_integer_residue(tmp_path):
    path = mutate_file(tmp_path, lambda lines: [
        line[:26] + "A" + line[27:] if line.startswith("ATOM  ") and line[22:26].strip() == "773" else line
        for line in lines])
    changed = mapping()
    changed["author_residue_id"] = "773A"
    assert audit_structure_site(path, changed, "AQ4")["cys_author_residue"] == "773A"
    with pytest.raises(ValueError):
        audit_structure_site(path, mapping(), "AQ4")


def test_explicit_link_does_not_pass_noncovalent_check(tmp_path):
    def transform(lines):
        sg = next(int(line[6:11]) for line in lines if line.startswith("ATOM  ")
                  and line[22:26].strip() == "773" and line[12:16].strip() == "SG")
        ligand = next(int(line[6:11]) for line in lines if line[17:20] == "AQ4")
        return lines + [f"CONECT{sg:5d}{ligand:5d}"]
    result = audit_structure_site(mutate_file(tmp_path, transform), mapping(), "AQ4")
    assert result["status"] == "explicit_linkage_requires_review"
    assert not result["campaign_ready"]


def test_altloc_requires_selection_and_checks_atom_coverage(tmp_path):
    path = mutate_file(tmp_path, lambda lines: [
        line[:16] + "A" + line[17:] if line[17:20] == "AQ4" else line for line in lines])
    with pytest.raises(ValueError, match="Alternate"):
        audit_structure_site(path, mapping(), "AQ4")
    assert audit_structure_site(path, mapping(), "AQ4", altloc="A")["ligand_heavy_atom_count"] == 29
    with pytest.raises(ValueError, match="absent"):
        audit_structure_site(path, mapping(), "AQ4", altloc="B")


def test_zero_occupancy_rejected(tmp_path):
    path = mutate_file(tmp_path, lambda lines: [
        line[:54] + "  0.00" + line[60:] if line[17:20] == "AQ4" else line for line in lines])
    with pytest.raises(ValueError, match="zero-occupancy"):
        audit_structure_site(path, mapping(), "AQ4")


def test_multiple_models_never_merge(tmp_path):
    def transform(lines):
        header = [line for line in lines if line.startswith("HEADER")]
        atoms = [line for line in lines if line.startswith(("ATOM  ", "HETATM"))]
        return header + ["MODEL        1"] + atoms + ["ENDMDL", "MODEL        2"] + atoms + ["ENDMDL"]
    path = mutate_file(tmp_path, transform)
    with pytest.raises(ValueError, match="Multiple"):
        audit_structure_site(path, mapping(), "AQ4")
    assert audit_structure_site(path, mapping(), "AQ4", model=2)["ligand_heavy_atom_count"] == 29


def test_tool_preserves_mapping_hash_and_does_not_write(tmp_path):
    from covscreen.tool_api import ligand_first_audit_structure_site_tool
    import hashlib
    pdb = tmp_path / "1M17.pdb"
    pdb.write_bytes(SOURCE.read_bytes())
    mapped = tmp_path / "mapping.json"
    mapped.write_text(json.dumps(mapping()))
    before = set(tmp_path.iterdir())
    result = ligand_first_audit_structure_site_tool(tmp_path, "1M17.pdb", "mapping.json", "AQ4")
    assert result["audit"]["mapping_sha256"] == hashlib.sha256(mapped.read_bytes()).hexdigest()
    assert not result["calculation_submitted"]
    assert before == set(tmp_path.iterdir())
    with pytest.raises(ValueError):
        ligand_first_audit_structure_site_tool(tmp_path, "../outside.pdb", "mapping.json", "AQ4")


def test_wrong_ligand_selector_does_not_claim_missing_cys():
    with pytest.raises(ValueError, match="Cys SG IS present.*999"):
        audit_structure_site(SOURCE, mapping(), "AQ4", ligand_chain="A", ligand_residue="501")


@pytest.mark.parametrize('modified', [False, True])
def test_missing_cys_sg_reports_observed_labels_without_inferring_chemistry(tmp_path, modified):
    def transform(lines):
        result = []
        for line in lines:
            target = line.startswith('ATOM  ') and line[22:26].strip() == '773'
            if target and modified:
                line = 'HETATM' + line[6:17] + 'CSO' + line[20:]
            elif target and line[12:16].strip() == 'SG':
                continue
            result.append(line)
        return result
    with pytest.raises(ValueError, match='Required CYS SG coordinate not found') as error:
        audit_structure_site(mutate_file(tmp_path, transform), mapping(), 'AQ4')
    message = str(error.value)
    assert 'does not establish' in message
    if modified:
        assert "('CSO', 'SG')" in message
    else:
        assert "('CYS', 'CA')" in message
        assert "('CYS', 'SG')" not in message
