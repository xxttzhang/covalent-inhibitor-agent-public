import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from covscreen.adapters.base import EngineAdapter
from covscreen.research import (
    LibraryFirstRequest,
    LibraryFirstResult,
    TargetPassport,
    validate_library_first_request,
    validate_project,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("project_id", "decision", "route"),
    [
        ("egfr-c797", "GO", "ligand-first"),
        ("kras-g12c", "GO", "ligand-first"),
        ("kras-wt-g12-control", "STOP", "evidence-first"),
    ],
)
def test_non_btk_acceptance_projects(project_id, decision, route):
    project_dir = ROOT / "projects" / project_id
    assert validate_project(project_dir)["status"] == "PASS"
    assessment = json.loads(
        (project_dir / "assessment/covalent_assessment.json").read_text()
    )
    plan = json.loads((project_dir / "plans/route_plan.json").read_text())
    assert assessment["decision"] == decision
    assert plan["selected_route"] == route
    assert project_id.lower().find("btk") == -1


def test_target_passports_have_three_source_classes():
    for project_dir in (ROOT / "projects").iterdir():
        passport = TargetPassport.model_validate(
            json.loads((project_dir / "target_passport.json").read_text())
        )
        assert {item.source_type for item in passport.citations} >= {
            "uniprot",
            "pdb",
            "publication",
        }


def test_library_first_request_validates_exact_site(tmp_path):
    source = ROOT / "projects" / "egfr-c797"
    project_dir = tmp_path / "egfr-c797"
    project_dir.mkdir()
    (project_dir / "target_passport.json").write_text(
        (source / "target_passport.json").read_text()
    )
    (project_dir / "receptor.pdb").write_text("HEADER CONTRACT TEST ONLY\n")
    (project_dir / "library.csv").write_text(
        "compound_id,standardized_smiles\ncontract-1,CC\n"
    )
    request = LibraryFirstRequest(
        request_id="req-egfr-contract",
        project_id="egfr-c797",
        target_passport_path="target_passport.json",
        receptor_path="receptor.pdb",
        ligand_table_path="library.csv",
        site_id="EGFR_C797",
        chain_id="A",
        residue_name="CYS",
        residue_number=797,
        reactive_atom="SG",
        reaction_type="Michael Addition",
        output_dir="runs/req-egfr-contract",
        engines=[{"engine": "carsidock-cov"}],
        resource_budget={
            "max_gpu_hours": 1,
            "max_wall_hours": 2,
            "max_concurrent_jobs": 1,
        },
    )
    assert validate_library_first_request(request, project_dir)["status"] == "PASS"
    invalid = request.model_copy(update={"residue_number": 481})
    result = validate_library_first_request(invalid, project_dir)
    assert result["status"] == "FAIL"
    assert "exactly match" in result["errors"][0]


def test_library_first_result_requires_full_denominator():
    with pytest.raises(ValidationError):
        LibraryFirstResult(
            request_id="r1",
            engine="example",
            input_count=10,
            success_count=8,
            failed_count=1,
            status="PARTIAL",
            score_direction="lower_is_better",
            provenance={},
        )


class DummyAdapter(EngineAdapter):
    name = "dummy"

    def check_availability(self):
        return {"ready": True}

    def build_command(self, input_file):
        return ["dummy", "--input", input_file]


def test_adapter_compiles_auditable_dry_run():
    plan = DummyAdapter().build_execution_plan(
        request_id="r1",
        input_paths=["library.csv"],
        output_dir="runs/r1",
        score_direction="lower_is_better",
        command_kwargs={"input_file": "library.csv"},
    )
    assert plan.dry_run is True
    assert plan.command == ["dummy", "--input", "library.csv"]


def test_generic_core_has_no_btk_default():
    for path in (
        ROOT / "src/covscreen/research.py",
        ROOT / "scripts/covalent_project.py",
    ):
        assert "BTK" not in path.read_text()
