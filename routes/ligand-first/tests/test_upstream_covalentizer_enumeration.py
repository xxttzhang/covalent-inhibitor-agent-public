"""Exercise the actual upstream enumeration function, not a mock generator."""
import importlib.util
import os
import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = (Path(__file__).resolve().parents[3] / "science")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def modules(monkeypatch):
    repo = Path(os.environ.get("COVALENTIZER_TEST_REPO", str(ROOT / "external/Covalentizer")))
    if not (repo / "Code/CovUtils.py").is_file():
        pytest.skip("upstream Covalentizer checkout required for integration test")
    monkeypatch.setenv("COVALENTIZER", str(repo))
    return (load(ROOT / "scripts/run_upstream_covalentizer_enumeration.py", "adapter"),
            load(repo / "Code/CovUtils.py", "covutils_test"), repo)


def test_duplicate_fragments_keep_both_sources(modules, tmp_path):
    adapter, upstream, repo = modules
    fragments = tmp_path / "inputs.smi"
    fragments.write_text("c1ccccc1 parent_A_fragment\nc1ccccc1 parent_B_fragment\n")
    rows = adapter.enumerate_traced(upstream, repo / "Code/numbered_reaction.re",
                                    repo / "Scripts/reform_elec.re", fragments, tmp_path)
    assert len(rows) == 16
    assert len({row["candidate_id"] for row in rows}) == 16
    assert len({row["structure_sha256"] for row in rows}) == 8
    for parent in ("parent_A_fragment", "parent_B_fragment"):
        assert sum(row["input_id"] == parent for row in rows) == 8
    assert all(row["chemical_gate"] == "PASS" for row in rows)
    assert all(Path(row["source_product_file"]).is_file() for row in rows)


def test_invalid_input_is_not_silently_dropped(modules, tmp_path):
    adapter, upstream, repo = modules
    fragments = tmp_path / "inputs.smi"
    fragments.write_text("c1ccccc1 valid\nnot_a_smiles invalid\n")
    with pytest.raises(ValueError, match="input line 2"):
        adapter.enumerate_traced(upstream, repo / "Code/numbered_reaction.re",
                                 repo / "Scripts/reform_elec.re", fragments, tmp_path)
    assert not list(tmp_path.glob("fragment_*"))


def test_real_upstream_products_bind_full_parent_without_losing_atoms(modules, tmp_path):
    from rdkit import Chem
    from covscreen.upstream_handoff import bind_full_parent_atoms, identify_warhead

    adapter, upstream, repo = modules
    parent = Chem.MolFromSmiles('COc1ncccc1F')
    fragments = tmp_path / 'inputs.smi'
    fragments.write_text('COc1ncccc1F asymmetric_parent\n')
    rows = adapter.enumerate_traced(upstream, repo / 'Code/numbered_reaction.re',
                                    repo / 'Scripts/reform_elec.re', fragments, tmp_path)
    accepted = []
    for row in rows:
        product = Chem.MolFromSmiles(row['smiles'])
        if identify_warhead(product)['warhead_class'] != 'acrylamide':
            continue
        bound, evidence = bind_full_parent_atoms(parent, product)
        restored = Chem.MolFromMolBlock(Chem.MolToMolBlock(bound))
        assert len(evidence['parent_atom_index_to_product_index']) == parent.GetNumAtoms()
        assert all(atom.GetAtomMapNum() > 0 for atom in restored.GetAtoms())
        assert len({atom.GetAtomMapNum() for atom in restored.GetAtoms()}) == restored.GetNumAtoms()
        assert evidence['reaction_ancestry_verified'] is False
        assert evidence['pose_assessment'] == 'NOT_RUN'
        accepted.append(evidence)
    assert accepted, 'Actual upstream acrylamide products must exercise the binder'


def test_native_campaign_without_manual_annotations_runs_real_enumerator(modules, tmp_path, monkeypatch):
    """In-process CLI dispatch uses real CovUtils; no candidate generator mock."""
    adapter, _, repo = modules
    campaign_dir = tmp_path / "campaign"
    source = Path(os.environ.get("COVALENTIZER_TEST_CAMPAIGN", str(ROOT / "projects/egfr-c797/ligand_first")))
    shutil.copytree(source, campaign_dir)
    path = campaign_dir / "campaign.json"
    payload = json.loads(path.read_text())
    payload["planning_mode"] = "upstream-native"
    for key in ("protection", "covalent_references", "exit_vectors"):
        payload.pop(key)
    path.write_text(json.dumps(payload))
    runner = load(ROOT / "scripts/run_upstream_ligand_campaign.py", "native_campaign_test")
    native_subprocess_run = runner.subprocess.run
    out = tmp_path / "enumerated"

    def dispatch(command, *, check):
        assert check and Path(command[1]).name == "run_upstream_covalentizer_enumeration.py"
        with monkeypatch.context() as context:
            context.setattr(sys, "argv", command[1:])
            assert adapter.main() == 0

    monkeypatch.setattr(runner.subprocess, "run", dispatch)
    monkeypatch.setattr(sys, "argv", ["run", "--campaign", str(path),
                        "--upstream-repo", str(repo), "--output-dir", str(out),
                        "--max-candidates", "512"])
    assert runner.main() == 0
    result = json.loads((out / "ligand_first_candidates.json").read_text())
    assert result["status"] == "prepared"
    assert result["chemistry_pass_count"] == 108
    assert result["candidate_interaction_retention_status"] == "NOT_ASSESSED"
    assert result["planning_mode"] == "upstream-native"
    audit = json.loads((out / "input_audit.json").read_text())
    assert audit["protected_interactions"] == []
    table = runner.pd.read_csv(out / "ligand_first_candidates.csv")
    assert table.warhead_class.value_counts().to_dict() == {
        "acrylamide": 27, "chloroacetamide": 27,
        "propiolamide": 27, "vinyl_sulfonamide": 27,
    }
    assert table.parent_retained_fraction.eq(1.0).all()

    # Recheck actual upstream products against the same measured protection
    # heuristic used by webpage designs. This remains an isolated fixture,
    # not a registered research candidate or a binding-pose assessment.
    from rdkit import Chem
    from covscreen.ligand_design import protection
    from covscreen.ligand_first_inputs import load_campaign, build_parent_pose_mol, ProtectionSpec
    from covscreen.rdkit_covalentizer import evaluate_chemistry_quality
    from covscreen.upstream_handoff import (bind_full_parent_atoms, identify_warhead,
                                           full_parent_attachment_geometry, select_bounded_additions)
    from covscreen.ligand_first_site_discovery import _target_xyz
    from covscreen.ligand_design import POLICY

    campaign = load_campaign(path)
    parent = build_parent_pose_mol(campaign, campaign_dir)
    measured = protection(parent, campaign, campaign_dir)
    campaign = campaign.model_copy(update={
        'allowed_warheads': ['acrylamide'],
        'protection': ProtectionSpec(**{key: measured[key] for key in (
            'scaffold_core_atom_names', 'protected_interactions',
            'protected_pharmacophore_atom_names')})})
    assert campaign.protection.scaffold_core_atom_names
    assert campaign.protection.protected_pharmacophore_atom_names
    enumeration = json.loads((out / 'upstream_enumeration_manifest.json').read_text())
    accepted, rejected, geometry_reviews = [], [], []
    for row in enumeration['candidates']:
        product = Chem.MolFromSmiles(row['smiles'])
        if identify_warhead(product)['warhead_class'] != 'acrylamide':
            continue
        try:
            bound, evidence = bind_full_parent_atoms(parent, product)
        except ValueError as error:
            rejected.append(str(error))
            continue
        restored = Chem.MolFromMolBlock(Chem.MolToMolBlock(bound))
        gate = evaluate_chemistry_quality(restored, parent, campaign)
        assert gate.status == 'PASS', gate.failure_reasons
        # A producer PASS cannot hide a missing protected atom mapping when
        # the saved molecule is independently reviewed downstream.
        damaged = Chem.Mol(restored)
        protected_name = campaign.protection.protected_pharmacophore_atom_names[0]
        protected_index = next(atom.GetIdx() for atom in parent.GetAtoms()
                               if atom.GetProp('pdb_atom_name') == protected_name)
        damaged.GetAtomWithIdx(evidence['parent_atom_index_to_product_index'][protected_index]).SetAtomMapNum(0)
        damaged_gate = evaluate_chemistry_quality(damaged, parent, campaign)
        assert damaged_gate.status == 'FAIL'
        assert not damaged_gate.protected_pharmacophore_retained
        assert evidence['pose_assessment'] == 'NOT_RUN'
        geometry_reviews.append(full_parent_attachment_geometry(
            parent, evidence['attachment_parent_atom_index'], _target_xyz(campaign, campaign_dir),
            max_distance_angstrom=POLICY['max_distance_angstrom'],
            max_vector_angle_degrees=POLICY['max_vector_angle_degrees']))
        accepted.append(row['candidate_id'])
    assert len(accepted) + len(rejected) == 27
    assert accepted, rejected
    reviewed = [{'candidate_id': cid, 'geometry': geometry}
                for cid, geometry in zip(accepted, geometry_reviews)]
    selection = select_bounded_additions(reviewed, max_sites=POLICY['max_sites'],
                                        max_candidates=POLICY['max_candidates'])
    assert selection == select_bounded_additions(list(reversed(reviewed)),
        max_sites=POLICY['max_sites'], max_candidates=POLICY['max_candidates'])
    assert 0 < len(selection['selected_candidate_ids']) <= 6
    assert len(selection['budgeted_parent_atom_indices']) <= 2
    assert {row['candidate_id'] for row in selection['inventory']} == set(accepted)
    assert len(selection['inventory']) == len(accepted)
    by_id = {row['candidate_id']: row for row in reviewed}
    assert all(by_id[cid]['geometry']['prefilter_passed'] for cid in selection['selected_candidate_ids'])
    small = select_bounded_additions(reviewed, max_sites=1, max_candidates=1)
    assert len(small['selected_candidate_ids']) == 1
    assert any(row['disposition'] == 'deferred_candidate_budget' for row in small['inventory'])
    assert any(row['disposition'] == 'deferred_site_budget' for row in small['inventory'])
    with pytest.raises(ValueError, match='unique'):
        select_bounded_additions(reviewed + reviewed[:1], max_sites=2, max_candidates=6)
    from covscreen.upstream_handoff import upstream_candidate_record
    from covscreen.rdkit_covalentizer import CovalentizedCandidate
    from covscreen.ligand_candidate_table import candidate_table_row
    from covscreen.jobs import sha256_file
    source_rows = {row['candidate_id']: row for row in enumeration['candidates']}
    source_hash = sha256_file(out / 'upstream_enumeration_manifest.json')
    from covscreen.upstream_handoff import review_full_parent_enumeration
    expected_inputs = {str(path.resolve()): sha256_file(path) for path in (
        repo / 'Code/CovUtils.py', repo / 'Code/numbered_reaction.re',
        repo / 'Scripts/reform_elec.re', out / 'upstream_parent.smi')}
    records, molecules, audit = review_full_parent_enumeration(
        out / 'upstream_enumeration_manifest.json', parent, campaign,
        _target_xyz(campaign, campaign_dir), expected_input_sha256=expected_inputs, policy=POLICY)
    assert len(records) == len(molecules) == 4
    assert len(audit['inventory']) == audit['source_candidate_count'] == 108
    assert {item['candidate_id'] for item in audit['inventory']} == set(source_rows)
    assert sum(item['disposition'] == 'outside_warhead_policy' for item in audit['inventory']) == 81
    assert [record.candidate_id for record in records] == list(audit['selected_source_to_candidate_id'].values())
    source_product = Path(enumeration['candidates'][0]['source_product_file'])
    original_bytes = source_product.read_bytes()
    try:
        source_product.write_bytes(original_bytes + b'\n')
        with pytest.raises(ValueError, match='source digest'):
            review_full_parent_enumeration(out / 'upstream_enumeration_manifest.json', parent, campaign,
                _target_xyz(campaign, campaign_dir), expected_input_sha256=expected_inputs, policy=POLICY)
    finally:
        source_product.write_bytes(original_bytes)
    wrong_inputs = dict(expected_inputs)
    wrong_inputs[str((out / 'upstream_parent.smi').resolve())] = '0' * 64
    with pytest.raises(ValueError, match='expected parent'):
        review_full_parent_enumeration(out / 'upstream_enumeration_manifest.json', parent, campaign,
            _target_xyz(campaign, campaign_dir), expected_input_sha256=wrong_inputs, policy=POLICY)
    from covscreen.upstream_handoff import generate_full_parent_candidates
    with monkeypatch.context() as context:
        context.setattr(runner.subprocess, 'run', native_subprocess_run)
        native_records, native_molecules, native_audit = generate_full_parent_candidates(
            parent, campaign, _target_xyz(campaign, campaign_dir), tmp_path / 'native-adapter',
            upstream_repo=repo, policy=POLICY)
    assert len(native_records) == len(native_molecules) == 4
    assert native_audit['source_candidate_count'] == 108
    assert {r.canonical_smiles for r in native_records} == {r.canonical_smiles for r in records}
    assert (tmp_path / 'native-adapter/enumeration.log').is_file()
    for index, source_id in enumerate(selection['selected_candidate_ids']):
        record, molecule = upstream_candidate_record(parent, campaign, source_rows[source_id],
            manifest_sha256=source_hash, input_sha256=enumeration['input_sha256'])
        restored_record = CovalentizedCandidate.model_validate_json(record.model_dump_json())
        assert restored_record == record
        assert record.upstream_source.enumeration_candidate_id == source_id
        assert record.upstream_source.enumeration_manifest_sha256 == source_hash
        assert record.provenance == 'upstream-covalentizer-full-parent-v1'
        assert not record.upstream_source.reaction_ancestry_verified
        assert molecule.GetProp('candidate_id') == record.candidate_id
        downstream = candidate_table_row(record, campaign, index)
        assert downstream['compound_id'] == record.candidate_id
        missing = record.model_dump()
        missing.pop('upstream_source')
        with pytest.raises(ValueError, match='source evidence'):
            CovalentizedCandidate.model_validate(missing)
        mislabeled = record.model_dump()
        mislabeled['provenance'] = 'deterministic-rdkit-covalentizer'
        with pytest.raises(ValueError, match='legacy label'):
            CovalentizedCandidate.model_validate(mislabeled)
        changed = dict(source_rows[source_id], structure_sha256='0' * 64)
        with pytest.raises(ValueError, match='digest'):
            upstream_candidate_record(parent, campaign, changed,
                manifest_sha256=source_hash, input_sha256=enumeration['input_sha256'])
    print(json.dumps({'full_parent_adapter_accepted': len(accepted),
                      'full_parent_adapter_rejected': len(rejected),
                      'rejection_reasons': sorted(set(rejected)),
                      'protected_contact_atoms': campaign.protection.protected_pharmacophore_atom_names,
                      'geometry_prefilter_pass_count': sum(row['prefilter_passed'] for row in geometry_reviews),
                      'geometry_prefilter_pass_anchors': sorted({row['attachment_parent_atom_index']
                          for row in geometry_reviews if row['prefilter_passed']}),
                      'budget_selected_count': len(selection['selected_candidate_ids']),
                      'budget_selected_anchors': selection['budgeted_parent_atom_indices'],
                      'budget_inventory_count': len(selection['inventory']),
                      'pose_assessment': 'NOT_RUN'}))
