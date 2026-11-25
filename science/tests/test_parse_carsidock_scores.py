import importlib.util
from pathlib import Path

import pandas as pd
import pytest
from rdkit import Chem
from rdkit.Chem import AllChem


SCRIPT = Path(__file__).parents[1] / "scripts" / "parse_carsidock_scores.py"
spec = importlib.util.spec_from_file_location("parse_carsidock_scores", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def make_molecule(input_index: int, compound_id: str, score=None):
    molecule = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    AllChem.EmbedMolecule(molecule, randomSeed=input_index + 1)
    molecule.SetProp("INPUT_SDF_INDEX", str(input_index))
    molecule.SetProp("COMPOUND_ID", compound_id)
    if score is not None:
        molecule.SetProp("CarsiScore", str(score))
        molecule.SetProp("loss", "0.1")
        molecule.SetProp("score", "1.0")
    return molecule


def write_molecules(path, items):
    writer = Chem.SDWriter(str(path))
    for item in items:
        writer.write(item)
    writer.close()


def test_parse_chunk_maps_first_pose_and_keeps_missing_input(tmp_path):
    table = pd.DataFrame(
        {
            "compound_id": ["a", "b", "c"],
            "input_sdf_index": [10, 11, 12],
            "label": ["active", "decoy", "decoy"],
        }
    )
    table_path = tmp_path / "chunk_0000.parquet"
    table.to_parquet(table_path)
    input_sdf = tmp_path / "chunk_0000.sdf"
    write_molecules(
        input_sdf,
        [make_molecule(10, "a"), make_molecule(11, "b")],
    )
    prefix = tmp_path / "result"
    poses = []
    for index, compound in ((10, "a"), (11, "b")):
        poses.extend(
            [
                make_molecule(index, compound, score=index),
                make_molecule(index, compound, score=99),
            ]
        )
    write_molecules(prefix.with_suffix(".sdf"), poses)
    write_molecules(Path(f"{prefix}_nodumat.sdf"), poses)
    Path(f"{prefix}_withpckt.pdb").write_text("ATOM\n")

    result, audit = module.parse_chunk(table_path, input_sdf, prefix)
    assert result["raw_score"].tolist()[:2] == [10.0, 11.0]
    assert result.loc[2, "engine_status"] == "FAILED"
    assert "chemistry_preparation" in result.loc[2, "failure_reason"]
    assert audit["poses_per_input"] == 2


def test_parse_chunk_maps_protomer_expansion_by_longest_compound_id(tmp_path):
    table = pd.DataFrame(
        {
            "compound_id": ["a-b", "a"],
            "input_sdf_index": [0, 1],
            "label": ["unknown", "unknown"],
        }
    )
    table_path = tmp_path / "chunk_0000.parquet"
    table.to_parquet(table_path)
    input_sdf = tmp_path / "chunk_0000.sdf"
    write_molecules(
        input_sdf,
        [make_molecule(0, "a-b"), make_molecule(1, "a")],
    )
    poses = []
    for variant, scores in (
        ("a-b-n-0", (5.0, 1.0)),
        ("a-b-n-1", (3.0, 4.0)),
        ("a-n-0", (7.0, 6.0)),
    ):
        for score in scores:
            molecule = make_molecule(0, "unused", score=score)
            molecule.SetProp("_Name", variant)
            poses.append(molecule)
    prefix = tmp_path / "result"
    write_molecules(prefix.with_suffix(".sdf"), poses)
    write_molecules(Path(f"{prefix}_nodumat.sdf"), poses)
    Path(f"{prefix}_withpckt.pdb").write_text("ATOM\n")

    result, audit = module.parse_chunk(table_path, input_sdf, prefix)

    assert audit["identity_mapping_available"] is True
    assert audit["mapped_input_count"] == 2
    assert audit["variant_count"] == 3
    assert audit["poses_per_variant"] == 2
    assert audit["poses_per_input"] is None
    assert audit["status"] == "SUCCESS"
    assert result["raw_score"].tolist() == [1.0, 6.0]
    assert result["selected_pose_record_index"].tolist() == [1, 5]
    assert result["pose_variant_id"].tolist() == ["a-b-n-0", "a-n-0"]
    assert result["pose_count"].tolist() == [4, 2]
    assert result["protomer_count"].tolist() == [2, 1]
    assert result["engine_status"].tolist() == ["SUCCESS", "SUCCESS"]


@pytest.mark.parametrize("valid_score", [None, 2.0])
def test_infinite_pose_is_never_selected_or_successful(tmp_path, valid_score):
    table = tmp_path / "input.parquet"
    pd.DataFrame({"compound_id": ["a"], "input_sdf_index": [0]}).to_parquet(table)
    source = tmp_path / "input.sdf"
    write_molecules(source, [make_molecule(0, "a")])
    prefix = tmp_path / "result"
    poses = [make_molecule(0, "a", score=-float("inf"))]
    if valid_score is not None:
        poses.append(make_molecule(0, "a", score=valid_score))
    for pose in poses:
        pose.SetProp("_Name", "a")
    write_molecules(prefix.with_suffix(".sdf"), poses)
    write_molecules(Path(f"{prefix}_nodumat.sdf"), poses)
    Path(f"{prefix}_withpckt.pdb").write_text("ATOM\n")
    result, _ = module.parse_chunk(table, source, prefix)
    assert result.loc[0, "engine_status"] == ("FAILED" if valid_score is None else "SUCCESS")
    if valid_score is not None:
        assert result.loc[0, "raw_score"] == valid_score
        assert result.loc[0, "selected_pose_record_index"] == 1
