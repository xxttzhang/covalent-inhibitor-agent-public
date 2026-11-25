from covscreen.covalent_enumerator import CovalentEnumerationRequest, enumerate_covalent_candidates


def test_deterministic_enumerator_attaches_requested_warheads_with_traceability():
    result = enumerate_covalent_candidates(
        CovalentEnumerationRequest(
            request_id="enumeration-fixture",
            scaffold_smiles="c1ccccc1",
            attachment_atom_indices=[0],
            allowed_warheads=["acrylamide", "propiolamide"],
        )
    )
    assert result.status == "PASS"
    assert [candidate.warhead_name for candidate in result.candidates] == ["acrylamide", "propiolamide"]
    assert all(candidate.provenance == "deterministic-rdkit-enumeration" for candidate in result.candidates)
    assert all(candidate.scaffold_attachment_atom_index == 0 for candidate in result.candidates)


def test_deterministic_enumerator_keeps_invalid_inputs_as_explicit_failures():
    result = enumerate_covalent_candidates(
        CovalentEnumerationRequest(
            request_id="bad-scaffold",
            scaffold_smiles="not-a-smiles",
            attachment_atom_indices=[0],
            allowed_warheads=["acrylamide"],
        )
    )
    assert result.status == "FAIL"
    assert result.candidates == []
    assert "not RDKit-parseable" in result.rejected[0]


def test_formal_four_warheads_and_short_linkers_are_supported():
    result = enumerate_covalent_candidates(
        CovalentEnumerationRequest(
            request_id="formal-four-warheads",
            scaffold_smiles="c1ccccc1",
            attachment_atom_indices=[0],
            allowed_warheads=[
                "acrylamide",
                "chloroacetamide",
                "vinyl_sulfone",
                "propiolamide",
            ],
            allowed_linkers=["direct", "methylene", "ethylene"],
        )
    )
    assert result.status == "PASS"
    assert len(result.candidates) == 12
    assert {candidate.warhead_name for candidate in result.candidates} == {
        "acrylamide",
        "chloroacetamide",
        "vinyl_sulfone",
        "propiolamide",
    }
    assert {candidate.linker_name for candidate in result.candidates} == {
        "direct",
        "methylene",
        "ethylene",
    }
    assert all(candidate.chemistry_gate_pass for candidate in result.candidates)


def test_attachment_atoms_can_be_selected_by_conservative_rdkit_rules():
    result = enumerate_covalent_candidates(
        CovalentEnumerationRequest(
            request_id="automatic-sites",
            scaffold_smiles="Cc1ccccc1",
            allowed_warheads=["acrylamide"],
            protected_atom_indices=[0],
        )
    )
    assert result.status == "PASS"
    assert result.candidates
    assert all(candidate.scaffold_attachment_atom_index != 0 for candidate in result.candidates)
