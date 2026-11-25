from pathlib import Path

import pandas as pd
import pytest

from covscreen.candidate_delivery import build_candidate_results_manifest


def _table(tmp_path: Path) -> Path:
    covdock = tmp_path / "all-poses.maegz"
    covdock.write_bytes(b"covdock")
    rows = []
    for index in range(3):
        af3 = tmp_path / f"candidate-{index}.cif"
        af3.write_text(f"candidate {index}\n")
        rows.append({
            "compound_id": f"candidate-{index}", "input_sdf_index": index,
            "standardized_smiles": "C=CC(=O)N", "target_id": "EGFR",
            "site_id": "A:CYS:797", "parent_id": "erlotinib",
            "warhead_class": "acrylamide", "electrophilic_atom": "C1",
            "nucleophilic_atom": "A:CYS:797:SG", "af3_success": True,
            "af3_mpae": 1.0 + index / 10, "model_cif": str(af3),
            "covdock_success": True, "covdock_score": -9.0 + index,
            "covdock_pose_file": str(covdock), "covdock_pose_index": index,
            "covdock_gate_detail": ["A", "B1", "C"][index],
            "final_rank": index + 1, "pareto_front": 1,
            "ranking_policy": "ligand-first-pareto-v1.0",
            "selection_reason": "physical gate; Pareto front; scaffold diversity",
            "independent_gate": "PASS",
            "independent_parent_mcs_fraction": 0.8,
        })
    path = tmp_path / "candidates.parquet"
    pd.DataFrame(rows).to_parquet(path, index=False)
    return path


def test_three_candidates_export_with_deduplicated_immutable_assets(tmp_path):
    selection = tmp_path / "stage6_selection_report.md"
    trace = tmp_path / "ligand_first_traceability_report.md"
    trace_json = tmp_path / "ligand_first_traceability_report.json"
    selection.write_text("# Selection\n")
    trace.write_text("# Traceability\n")
    trace_json.write_text('{"schema_version":"1.0"}\n')
    result = build_candidate_results_manifest(
        _table(tmp_path), route="ligand-first", task_id="ligand-task-001",
        expected_count=3,
        documents={
            "selection_report": selection,
            "traceability_report": trace,
            "traceability_json": trace_json,
        },
    )
    assert result["schema"] == "candidate-results-v2"
    assert len(result["candidates"]) == 3
    assert len(result["assets"]) == 4
    assert set(result["documents"]) == {
        "selection_report", "traceability_report", "traceability_json",
    }
    assert result["documents"]["selection_report"]["mime_type"] == "text/markdown"
    assert all(len(item["sha256"]) == 64 for item in result["documents"].values())
    assert all(row["structure_source"] == "af3+covdock" for row in result["candidates"])
    assert [row["covdock_structure_pose"] for row in result["candidates"]] == ["0", "1", "2"]
    assert all(len(asset["sha256"]) == 64 for asset in result["assets"].values())
    assert all(row["ranking_policy"] == "ligand-first-pareto-v1.0" for row in result["candidates"])
    assert all(row["selection_reason"] for row in result["candidates"])
    assert all(row["independent_gate"] == "PASS" for row in result["candidates"])
    assert all(row["independent_parent_mcs_fraction"] == "0.80000000000000004" for row in result["candidates"])


def test_failed_score_cannot_be_numeric_zero(tmp_path):
    path = _table(tmp_path)
    frame = pd.read_parquet(path)
    frame.loc[0, "covdock_success"] = False
    frame.loc[0, "covdock_failure_reason"] = "no accepted pose"
    frame.loc[0, "covdock_score"] = 0.0
    frame.loc[0, "covdock_pose_file"] = ""
    frame.to_parquet(path, index=False)
    with pytest.raises(ValueError, match="numeric score"):
        build_candidate_results_manifest(path, route="ligand-first", task_id="ligand-task-001")


def test_unified_scientific_identity_is_mandatory(tmp_path):
    path = _table(tmp_path)
    frame = pd.read_parquet(path).drop(columns=["nucleophilic_atom"])
    frame.to_parquet(path, index=False)
    with pytest.raises(ValueError, match="unified scientific fields"):
        build_candidate_results_manifest(path, route="ligand-first", task_id="ligand-task-001")


@pytest.mark.parametrize('route', ['library-first', 'ligand-first', 'de-novo'])
def test_explicit_candidate_route_is_preserved(tmp_path, route):
    path = _table(tmp_path)
    frame = pd.read_parquet(path)
    frame['route'] = route
    frame.to_parquet(path, index=False)
    result = build_candidate_results_manifest(path, route=route, task_id='source-task-001')
    assert result['route'] == route
    assert len(result['candidates']) == 3


@pytest.mark.parametrize('source', ['library-first', 'de-novo', '', None])
def test_candidate_provenance_cannot_be_relabelled(tmp_path, source):
    path = _table(tmp_path)
    frame = pd.read_parquet(path)
    frame['route'] = 'ligand-first'
    frame.loc[1, 'route'] = source
    frame.to_parquet(path, index=False)
    with pytest.raises(ValueError, match='table route differs'):
        build_candidate_results_manifest(path, route='ligand-first', task_id='source-task-001')


@pytest.mark.parametrize('pose', ['1.5', '-0.5', '1.00000000000000001', 'NaN', 'Infinity', '1e99999'])
@pytest.mark.parametrize('extension', ['csv', 'parquet'])
def test_pose_identity_cannot_be_rounded_or_truncated(tmp_path, pose, extension):
    source = _table(tmp_path)
    frame = pd.read_parquet(source)
    frame['covdock_pose_index'] = frame['covdock_pose_index'].astype(str)
    frame.loc[0, 'covdock_pose_index'] = pose
    path = tmp_path / ('invalid.' + extension)
    if extension == 'csv':
        frame.to_csv(path, index=False)
    else:
        frame.to_parquet(path, index=False)
    with pytest.raises(ValueError, match='pose index'):
        build_candidate_results_manifest(path, route='ligand-first', task_id='source-task-001')


def test_conflicting_pose_aliases_do_not_silently_choose_one(tmp_path):
    path = _table(tmp_path)
    frame = pd.read_parquet(path)
    frame['pose_index'] = frame['covdock_pose_index']
    frame.loc[0, 'pose_index'] = 2
    frame.to_parquet(path, index=False)
    with pytest.raises(ValueError, match='pose index'):
        build_candidate_results_manifest(path, route='ligand-first', task_id='source-task-001')


@pytest.mark.parametrize('pose', ['0', '0.0', '0e0'])
def test_integral_pose_aliases_preserve_zero(tmp_path, pose):
    path = _table(tmp_path)
    frame = pd.read_parquet(path)
    frame['pose_index'] = frame['covdock_pose_index'].astype(str)
    frame.loc[0, 'pose_index'] = pose
    frame.to_parquet(path, index=False)
    result = build_candidate_results_manifest(path, route='ligand-first', task_id='source-task-001')
    assert result['candidates'][0]['covdock_structure_pose'] == '0'
