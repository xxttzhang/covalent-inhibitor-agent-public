"""Real table/SDF provenance tests; no docking or licensed executable runs."""
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest
from rdkit import Chem


def test_known_legacy_input_replays_without_rewriting_signed_policy(tmp_path, monkeypatch):
    from covscreen import ligand_covdock_input as adapter
    from covscreen.covdock_gate import LIGAND_ALGORITHM_VERSION, STRICT_ALGORITHM_VERSION
    from covscreen.jobs import sha256_file
    current = {'input_count': 2, 'attachment_residue': 'A:797', 'reaction_type': 'Michael Addition',
        'gate_algorithm_version': LIGAND_ALGORITHM_VERSION, 'implementation_sha256': sha256_file(Path(adapter.__file__)),
        'candidate_table': 'bound-original-candidates'}
    # The constructor boundary is isolated here; the native-plan test exercises
    # actual scientific input construction and signing separately.
    monkeypatch.setattr(adapter, 'build', lambda root, state: dict(current))
    original = {**current, 'gate_algorithm_version': STRICT_ALGORITHM_VERSION,
        'implementation_sha256': '1bc88b45ff30663c634e3463cae903d6ee27ab62fef9c8c5767b4cc7d01011da'}
    import hashlib
    def bind(payload):
        raw = adapter.encoded(payload); digest = hashlib.sha256(raw).hexdigest()
        path = tmp_path / 'state/ligand-covdock-inputs' / digest[:32] / 'input.json'
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
        return {'refinement_input': {'covdock_input': adapter.present(tmp_path, path, payload)}}
    state = bind(original)
    assert adapter.load(tmp_path, state) == original
    for changed in ({**original, 'candidate_table': 'different-candidates'},
                    {**original, 'implementation_sha256': '0' * 64}):
        with pytest.raises(ValueError, match='current study evidence'):
            adapter.load(tmp_path, bind(changed))


def module():
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        spec = importlib.util.spec_from_file_location("covdock_source_entry", scripts / "run_covdock_lo_registered.py")
        result = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(result)
        return result
    finally:
        sys.path.remove(str(scripts))


def prepare(tmp_path, source, extra=None):
    table = tmp_path / "input.parquet"
    values = {"input_sdf_index": [5, 9], "compound_id": ["one", "two"],
              "standardized_smiles": ["C=CC(=O)N", "C=CC(=O)NC"], "raw_score": [0.8, 0.2],
              "engine": ["rtmscore", "rtmscore"], "score_direction": ["higher_is_better"] * 2,
              "engine_status": ["SUCCESS"] * 2, **(extra or {})}
    pd.DataFrame(values).to_parquet(table, index=False)
    identity, sdf = tmp_path / "identity.parquet", tmp_path / "ligands.sdf"
    result = module().prepare_identity_and_sdf(table, identity, sdf, source)
    return result, identity, sdf


def test_core_input_preserves_scores_without_fabricating_af3_rank_or_properties(tmp_path):
    frame, identity, sdf = prepare(tmp_path, "core")
    assert not any(name.startswith("af3_") for name in frame)
    assert frame.core_input_raw_score.tolist() == [0.8, 0.2]
    assert frame.core_input_engine.tolist() == ["rtmscore", "rtmscore"]
    assert frame.covdock_input_score_source.tolist() == ["core", "core"]
    pd.testing.assert_frame_equal(pd.read_parquet(identity), frame)
    molecules = list(Chem.SDMolSupplier(str(sdf)))
    assert len(molecules) == 2 and all(molecule is not None for molecule in molecules)
    assert [molecule.GetIntProp("INPUT_SDF_INDEX") for molecule in molecules] == [5, 9]
    assert [molecule.GetProp("COMPOUND_ID") for molecule in molecules] == ["one", "two"]
    assert [Chem.MolToSmiles(molecule) for molecule in molecules] == ["C=CC(N)=O", "C=CC(=O)NC"]
    for molecule in molecules:
        assert molecule.GetProp("INPUT_SCORE_SOURCE") == "core"
        assert not molecule.HasProp("AF3_MPAE_RANK") and not molecule.HasProp("AF3_MPAE")
    # Use the actual CovDock result parser: upstream generic scores must not
    # collide with or be substituted for the new engine's score columns.
    inspection = tmp_path / "inspection.json"
    inspection.write_text(json.dumps({"input": "fixture.mae", "rows": [
        {"title": molecules[0].GetProp("_Name"), "pose_index": 0, "fixture_score": -8.0},
        {"title": molecules[1].GetProp("_Name"), "pose_index": 1, "fixture_score": -7.0},
    ]}))
    parsed = module().parse_inspection(inspection, identity, "fixture_score", "lower_is_better")
    assert parsed.raw_score.tolist() == [-8.0, -7.0]
    assert parsed.core_input_raw_score.tolist() == [0.8, 0.2]
    assert not any(name.startswith("af3_") for name in parsed)


def test_explicit_af3_input_retains_its_real_score_fields(tmp_path):
    frame, _, sdf = prepare(tmp_path, "af3", {"engine": ["alphafold3"] * 2})
    assert frame.af3_mpae.tolist() == [0.8, 0.2]
    assert frame.af3_mpae_rank.tolist() == [2, 1]
    assert [molecule.GetIntProp("AF3_MPAE_RANK") for molecule in Chem.SDMolSupplier(str(sdf))] == [2, 1]


@pytest.mark.parametrize("source,extra", [("core", {"af3_mpae_rank": [1, 2]}),
    ("core", {"core_input_raw_score": [1, 2]}), ("af3", {"af3_mpae": [1, 2]}), ("unknown", {})])
def test_ambiguous_sources_rejected_before_output(tmp_path, source, extra):
    with pytest.raises(ValueError):
        prepare(tmp_path, source, extra)
    assert not (tmp_path / "identity.parquet").exists()
    assert not (tmp_path / "ligands.sdf").exists()
