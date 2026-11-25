import pytest

from covscreen.upstream_parent_mapping import parent_mapping_evidence


def test_symmetry_is_explicit_not_a_unique_atom_identity():
    evidence = parent_mapping_evidence(
        "c1ccccc1", "c1ccccc1", "C=CC(=O)Nc1ccccc1", min_parent_fraction=1)
    assert evidence["status"] == "PASS"
    assert evidence["parent_retained_fraction"] == 1
    assert evidence["mapping_ambiguous"] is True
    assert len(evidence["fragment_to_parent_matches"]) == 12
    assert len(evidence["fragment_to_product_matches"]) == 12


def test_lost_parent_fraction_and_core_fail():
    evidence = parent_mapping_evidence(
        "c1ccccc1CCN", "c1ccccc1", "C=CC(=O)Nc1ccccc1",
        min_parent_fraction=0.8, protected_core_smarts="CCN")
    assert evidence["status"] == "FAIL"
    assert "PARENT_RETENTION_BELOW_THRESHOLD" in evidence["failure_reasons"]
    assert "PROTECTED_CORE_LOST" in evidence["failure_reasons"]


def test_truncated_mapping_cannot_pass():
    evidence = parent_mapping_evidence(
        "c1ccccc1", "c1ccccc1", "C=CC(=O)Nc1ccccc1",
        min_parent_fraction=1, max_matches=1)
    assert evidence["status"] == "FAIL"
    assert evidence["mapping_truncated"]


def test_stereochemical_inversion_is_not_retention():
    evidence = parent_mapping_evidence(
        "F[C@H](Cl)Br", "F[C@H](Cl)Br", "F[C@@H](Cl)Br", min_parent_fraction=1)
    assert "FRAGMENT_NOT_RETAINED_IN_PRODUCT" in evidence["failure_reasons"]


def test_bad_core_configuration_is_an_error():
    with pytest.raises(ValueError, match="does not occur"):
        parent_mapping_evidence("CC", "CC", "CCC", min_parent_fraction=1,
                                protected_core_smarts="c1ccccc1")
