from pathlib import Path

import pandas as pd
import pytest
from rdkit import Chem
from rdkit.Chem import AllChem

from covscreen.carsidock_results import audit_carsidock_outputs


def write_sdf(path: Path, count: int) -> None:
    writer = Chem.SDWriter(str(path))
    for index in range(count):
        molecule = Chem.AddHs(Chem.MolFromSmiles("CCO"))
        AllChem.EmbedMolecule(molecule, randomSeed=index + 1)
        writer.write(molecule)
    writer.close()


def test_audit_distinguishes_docking_from_scoring_failure(tmp_path):
    input_sdf = tmp_path / "input.sdf"
    prefix = tmp_path / "chunk"
    write_sdf(input_sdf, 2)
    write_sdf(prefix.with_suffix(".sdf"), 2)
    write_sdf(Path(f"{prefix}_nodumat.sdf"), 2)
    Path(f"{prefix}_withpckt.pdb").write_text("ATOM\n")
    audit = audit_carsidock_outputs(prefix, input_sdf)
    assert audit["docking_success"] is True
    assert audit["scoring_success"] is False
    assert audit["status"] == "DOCKING_SUCCESS_SCORING_FAILED"

    pd.DataFrame({"score": [0.1, 0.2]}).to_csv(
        Path(f"{prefix}_rtms.csv"), index=False
    )
    audit = audit_carsidock_outputs(prefix, input_sdf)
    assert audit["status"] == "SUCCESS"


def test_audit_accepts_multiple_poses_but_one_score_per_input(tmp_path):
    input_sdf = tmp_path / "input.sdf"
    prefix = tmp_path / "chunk"
    write_sdf(input_sdf, 2)
    write_sdf(prefix.with_suffix(".sdf"), 6)
    write_sdf(Path(f"{prefix}_nodumat.sdf"), 6)
    Path(f"{prefix}_withpckt.pdb").write_text("ATOM\n")
    pd.DataFrame({"score": [0.1, 0.2]}).to_csv(
        Path(f"{prefix}_rtms.csv"), index=False
    )
    audit = audit_carsidock_outputs(prefix, input_sdf)
    assert audit["poses_per_input"] == 3
    assert audit["status"] == "SUCCESS"


def test_empty_pose_output_is_reported_as_failure(tmp_path):
    input_sdf = tmp_path / "input.sdf"
    prefix = tmp_path / "empty"
    write_sdf(input_sdf, 1)
    prefix.with_suffix(".sdf").touch()
    Path(f"{prefix}_nodumat.sdf").touch()
    audit = audit_carsidock_outputs(prefix, input_sdf)
    assert audit["status"] == "DOCKING_FAILED"
    assert audit["pose_count"] == 0
    assert audit["docking_success"] is False


@pytest.mark.parametrize("score", [float("inf"), -float("inf"), float("nan")])
def test_nonfinite_carsi_and_rtm_scores_cannot_pass(tmp_path, score):
    source = tmp_path / "input.sdf"
    prefix = tmp_path / "chunk"
    molecule = Chem.MolFromSmiles("CCO")
    molecule.SetProp("COMPOUND_ID", "candidate")
    molecule.SetProp("_Name", "candidate")
    with Chem.SDWriter(str(source)) as writer:
        writer.write(molecule)
    molecule.SetProp("CarsiScore", str(score))
    for output in (prefix.with_suffix(".sdf"), Path(f"{prefix}_nodumat.sdf")):
        with Chem.SDWriter(str(output)) as writer:
            writer.write(molecule)
    Path(f"{prefix}_withpckt.pdb").write_text("ATOM\n")
    pd.DataFrame({"score": [score]}).to_csv(Path(f"{prefix}_rtms.csv"), index=False)
    result = audit_carsidock_outputs(prefix, source)
    assert result["finite_carsiscores"] == result["finite_scores"] == 0
    assert result["status"] == "DOCKING_SUCCESS_SCORING_FAILED"
