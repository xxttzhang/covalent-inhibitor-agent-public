from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from receptor_fixture import write_receptor

from covscreen.execution import (
    JobManager,
    _engine_environment,
    _select_candidate_table_path,
    _worker_environment,
    execute_persisted_job,
)
from covscreen.jobs import (
    ArtifactRecord,
    ArtifactRegistry,
    JobSpec,
    JobSubmissionRequest,
    ResourceBudget,
    RunResult,
    utc_now,
)
from covscreen.tool_api import get_job_candidates_tool


def budget() -> ResourceBudget:
    return ResourceBudget(max_runtime_minutes=1, max_candidates=10)


def rdkit_request(route: str = "library-first") -> JobSubmissionRequest:
    return JobSubmissionRequest(
        request_id=f"test-{route}",
        project_id="test-project",
        route=route,
        engine="rdkit-warhead-enumeration",
        parameters={
            "scaffold_smiles": "CCO",
            "attachment_atom_indices": [1],
            "allowed_warheads": ["acrylamide"],
        },
        resource_budget=budget(),
        execution_mode="plan",
    )


def test_execute_mode_requires_explicit_approval_and_note(tmp_path: Path):
    base = dict(
        run_id="test-run-000001",
        request_id="request-1",
        project_id="project-1",
        route="library-first",
        engine="rdkit-warhead-enumeration",
        command=[sys.executable, "-c", "pass"],
        working_directory=str(tmp_path),
        output_directory=str(tmp_path / "out"),
        resource_budget=budget(),
        execution_mode="execute",
    )
    with pytest.raises(ValidationError, match="human_approved"):
        JobSpec(**base)
    with pytest.raises(ValidationError, match="approval_note"):
        JobSpec(**base, human_approved=True, approval_note="short")
    approved = JobSpec(
        **base,
        human_approved=True,
        approval_note="Approved one-minute deterministic test.",
    )
    assert approved.execution_mode == "execute"


def test_registry_persists_and_hashes_artifacts(tmp_path: Path):
    registry = ArtifactRegistry(tmp_path)
    spec = JobSpec(
        run_id="test-run-000002",
        request_id="request-2",
        project_id="project-1",
        route="library-first",
        engine="rdkit-warhead-enumeration",
        command=[sys.executable, "-c", "pass"],
        working_directory=str(tmp_path),
        output_directory=str(tmp_path / "out"),
        resource_budget=budget(),
    )
    result = RunResult(
        run_id=spec.run_id,
        state="PLANNED",
        engine=spec.engine,
        route=spec.route,
        project_id=spec.project_id,
        submitted_at=utc_now(),
        message="planned",
        stdout_path=f"state/jobs/{spec.run_id}/stdout.log",
        stderr_path=f"state/jobs/{spec.run_id}/stderr.log",
    )
    job_dir = registry.create(spec, result)
    artifact_path = job_dir / "candidates.csv"
    artifact_path.write_text("compound_id,score\nc1,1.5\n")
    artifact = registry.register_file(spec.run_id, artifact_path, "candidate_table")
    assert artifact.sha256.startswith(tuple("0123456789abcdef"))
    assert artifact.size_bytes == artifact_path.stat().st_size
    assert registry.load_spec(spec.run_id) == spec
    assert registry.load_result(spec.run_id) == result


def test_worker_records_outputs_and_candidate_table(tmp_path: Path):
    registry = ArtifactRegistry(tmp_path)
    run_id = "test-run-000003"
    job_dir = registry.job_dir(run_id)
    output = job_dir / "artifacts" / "candidate_table.csv"
    code = (
        "from pathlib import Path; "
        f"p=Path({str(output)!r}); p.parent.mkdir(parents=True, exist_ok=True); "
        "p.write_text('compound_id,score\\nc1,1.5\\n')"
    )
    spec = JobSpec(
        run_id=run_id,
        request_id="request-3",
        project_id="project-1",
        route="library-first",
        engine="rdkit-warhead-enumeration",
        command=[sys.executable, "-c", code],
        working_directory=str(tmp_path),
        output_directory=str(output.parent),
        expected_output_paths=[str(output)],
        resource_budget=budget(),
        execution_mode="execute",
        human_approved=True,
        approval_note="Approved one-minute worker integration test.",
    )
    initial = RunResult(
        run_id=run_id,
        state="QUEUED",
        engine=spec.engine,
        route=spec.route,
        project_id=spec.project_id,
        submitted_at=utc_now(),
        message="queued",
        stdout_path=f"state/jobs/{run_id}/stdout.log",
        stderr_path=f"state/jobs/{run_id}/stderr.log",
    )
    registry.create(spec, initial)
    final = execute_persisted_job(tmp_path, run_id)
    assert final.state == "COMPLETED"
    assert final.return_code == 0
    assert final.engine_pid is not None
    assert final.candidate_table_path == str(output.relative_to(tmp_path))
    assert {artifact.role for artifact in final.artifacts} >= {"candidate_table", "log", "manifest"}


def test_candidate_table_prefers_recall_pool_over_failure_table():
    artifacts = [
        ArtifactRecord(
            artifact_id="sha256:" + "a" * 64,
            run_id="test-run-000004",
            role="score_table",
            path="state/jobs/test/artifacts/stage3_failures.parquet",
            media_type="application/vnd.apache.parquet",
            size_bytes=1,
            sha256="a" * 64,
        ),
        ArtifactRecord(
            artifact_id="sha256:" + "b" * 64,
            run_id="test-run-000004",
            role="score_table",
            path="state/jobs/test/artifacts/stage3_recall_pool.parquet",
            media_type="application/vnd.apache.parquet",
            size_bytes=1,
            sha256="b" * 64,
        ),
    ]

    selected = _select_candidate_table_path(
        "carsidock-pool-selection", artifacts
    )

    assert selected is not None
    assert selected.endswith("stage3_recall_pool.parquet")


@pytest.mark.parametrize('engine', ['carsidock-cov', 'rtmscore', 'alphafold3-mpae'])
def test_carsidock_worker_constructs_relocated_runtime_library_order(tmp_path: Path, monkeypatch, engine):
    shared = tmp_path / "shared"
    python = shared / "carsidock_gpu_env" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("fake")
    (shared / "python311_runtime" / "lib").mkdir(parents=True)
    (shared / "carsidock_runtime_libs" / "lib").mkdir(parents=True)
    spec = JobSpec(
        run_id="test-carsi-env-001",
        request_id="request-carsi-env",
        project_id="project-1",
        route="ligand-first",
        engine=engine,
        command=[str(python), "runner.py"],
        working_directory=str(tmp_path),
        output_directory=str(tmp_path / "out"),
        provenance={"python_executable": str(python)},
        resource_budget=budget(),
    )
    monkeypatch.setenv('PYTHONHOME', '/opt/conda')
    monkeypatch.setenv('PYTHONPATH', '/unrelated/site-packages')
    monkeypatch.setenv('LD_PRELOAD', '/unrelated/preload.so')
    environment = _engine_environment(spec, tmp_path)
    libraries = environment["LD_LIBRARY_PATH"].split(":")
    assert libraries[:2] == [
        str(shared / "python311_runtime" / "lib"),
        str(shared / "carsidock_runtime_libs" / "lib"),
    ]
    assert environment["DGLBACKEND"] == "pytorch"
    assert environment.get("HOME")
    assert environment['PYTHONHOME'] == str(shared / 'python311_runtime')
    assert environment['PYTHONPATH'].split(':') == [str((Path(__file__).resolve().parents[1] / 'src').resolve()),
        str(shared / 'carsidock_gpu_env/lib/python3.11/site-packages')]
    assert environment['PYTHONNOUSERSITE'] == '1'
    assert environment['PYTHONDONTWRITEBYTECODE'] == '1'
    assert 'LD_PRELOAD' not in environment
    assert environment['OMP_NUM_THREADS'] == str(spec.resource_budget.cpu_count)


def test_detached_worker_preserves_existing_home(tmp_path: Path):
    environment = _worker_environment(tmp_path)

    assert environment.get("HOME")
    assert environment["PYTHONPATH"].split(":", 1)[0] == str((Path(__file__).resolve().parents[1] / "src").resolve())


def test_rdkit_job_protocol_supports_all_three_routes_and_retry(tmp_path: Path):
    manager = JobManager(tmp_path)
    for route in ("library-first", "ligand-first", "de-novo"):
        result = manager.submit(rdkit_request(route))
        assert result.state == "PLANNED"
        spec = manager.registry.load_spec(result.run_id)
        assert spec.route == route
        assert spec.engine == "rdkit-warhead-enumeration"
        assert len(spec.input_sha256) == 1

    failed = result.model_copy(update={"state": "FAILED", "failure_reason": "test"})
    manager.registry.write_result(failed)
    retried = manager.retry(
        result.run_id,
        human_approved=False,
        approval_note="Compile a traceable retry plan only.",
        execution_mode="plan",
    )
    assert retried.parent_run_id == result.run_id
    assert retried.attempt == 2


def test_ligand_first_covalentization_is_registered_and_bounded(tmp_path: Path):
    campaign = tmp_path / "projects" / "egfr" / "ligand_first" / "campaign.json"
    campaign.parent.mkdir(parents=True)
    campaign.write_text("{}\n")
    manager = JobManager(tmp_path)
    request = JobSubmissionRequest(
        request_id="ligand-first-covalentization-plan",
        project_id="egfr",
        route="ligand-first",
        engine="ligand-first-covalentization",
        parameters={"campaign": str(campaign.relative_to(tmp_path)), "max_sites": 8},
        resource_budget=ResourceBudget(
            max_runtime_minutes=15,
            cpu_count=2,
            memory_gb=8,
            max_candidates=96,
        ),
    )
    spec = manager.build_spec(request)
    assert spec.provenance["attachment_planner_used"] is False
    assert spec.parameters["formal_warheads"] == [
        "acrylamide",
        "chloroacetamide",
        "vinyl_sulfone",
        "propiolamide",
    ]
    assert "--auto-discover-sites" in spec.command
    with pytest.raises(ValueError, match="ligand-first route"):
        manager.build_spec(request.model_copy(update={"route": "library-first"}))
    with pytest.raises(ValueError, match="max_candidates"):
        manager.build_spec(
            request.model_copy(
                update={
                    "resource_budget": request.resource_budget.model_copy(
                        update={"max_candidates": 95}
                    )
                }
            )
        )


def test_native_covalentizer_plan_uses_upstream_sources_and_budget(tmp_path: Path):
    campaign = tmp_path / "campaign.json"
    campaign.write_text("{}")
    for relative in ("Code/CovUtils.py", "Code/numbered_reaction.re", "Scripts/reform_elec.re"):
        path = tmp_path / "external/Covalentizer" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("plan-only fixture")
    request = JobSubmissionRequest(
        request_id="native-plan", project_id="egfr", route="ligand-first",
        engine="ligand-first-covalentization",
        parameters={"campaign": "campaign.json", "enumeration_backend": "upstream-native"},
        resource_budget=ResourceBudget(max_runtime_minutes=15, cpu_count=2, memory_gb=8, max_candidates=512),
    )
    spec = JobManager(tmp_path).build_spec(request)
    assert any("run_upstream_ligand_campaign.py" in arg for arg in spec.command)
    assert spec.command[-2:] == ["--max-candidates", "512"]
    assert spec.provenance["algorithm_version"] == "upstream-covalentizer-native-v1"
    assert "vinyl_sulfonamide" in spec.parameters["formal_warheads"]
    assert "--auto-discover-sites" not in spec.command


def test_ligand_first_structure_intake_is_registered_hashed_and_cpu_only(
    tmp_path: Path,
):
    campaign = tmp_path / "projects" / "egfr" / "ligand_first" / "campaign.json"
    source = campaign.parent / "source_structures" / "parent.pdb"
    source.parent.mkdir(parents=True)
    source.write_text("HEADER TEST\n")
    campaign.write_text(
        json.dumps(
            {
                "source_assets": [
                    {
                        "path": "source_structures/parent.pdb",
                        "source_url": "https://files.rcsb.org/download/TEST.pdb",
                        "sha256": "0" * 64,
                    }
                ]
            }
        )
    )
    manager = JobManager(tmp_path)
    request = JobSubmissionRequest(
        request_id="ligand-first-structure-intake-plan",
        project_id="egfr",
        route="ligand-first",
        engine="ligand-first-structure-intake",
        parameters={"campaign": str(campaign.relative_to(tmp_path))},
        resource_budget=ResourceBudget(
            max_runtime_minutes=15,
            cpu_count=1,
            memory_gb=4,
            max_candidates=1,
        ),
    )
    spec = manager.build_spec(request)
    assert spec.resource_budget.gpu_count == 0
    assert spec.parameters["restore_missing"] is True
    assert "prepare_ligand_first_inputs.py" in " ".join(spec.command)
    assert "--restore-missing" in spec.command
    assert len(spec.input_sha256) == 2
    assert spec.provenance["algorithm_version"] == "ligand-first-structure-intake-v1.0"
    assert "SHA-256" in spec.provenance["source_policy"]
    with pytest.raises(ValueError, match="ligand-first route"):
        manager.build_spec(request.model_copy(update={"route": "library-first"}))


def test_covindb_prescreen_job_is_library_first_and_budget_bounded(tmp_path: Path):
    source = tmp_path / "external" / "Virtual_Screening_Library.csv"
    source.parent.mkdir()
    source.write_text(",Source,CompoundType,SMILES,InChI_Key\n")
    target = tmp_path / "configs" / "covindb" / "target.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("target_id: TEST\n")
    manager = JobManager(tmp_path)
    manager.config = {
        "covindb": {"virtual_screening_library": str(source)},
        "runtime": {"random_seed": 7},
    }
    request = JobSubmissionRequest(
        request_id="covindb-pilot-plan",
        project_id="test",
        route="library-first",
        engine="covindb-prescreen",
        parameters={
            "target_config": "configs/covindb/target.yaml",
            "top_n": 500,
            "max_rows": 10_000,
            "chunk_size": 1000,
        },
        resource_budget=ResourceBudget(
            max_runtime_minutes=60,
            cpu_count=8,
            memory_gb=32,
            max_candidates=500,
        ),
    )
    spec = manager.build_spec(request)
    assert spec.engine == "covindb-prescreen"
    assert spec.parameters["seed"] == 7
    assert spec.parameters["max_rows"] == 10_000
    assert str(source) in spec.command
    assert spec.provenance["algorithm_version"] == "covindb-prescreen-v1.3"
    with pytest.raises(ValueError, match="library-first route"):
        manager.build_spec(request.model_copy(update={"route": "ligand-first"}))
    with pytest.raises(ValueError, match="max_candidates"):
        manager.build_spec(
            request.model_copy(
                update={
                    "parameters": {
                        "target_config": "configs/covindb/target.yaml",
                        "top_n": 501,
                        "max_rows": 10_000,
                    }
                }
            )
        )


def test_recall_pool_job_is_registered_identity_bounded_and_cpu_only(tmp_path: Path):
    import pandas as pd

    scores = tmp_path / "inputs" / "carsidock_scores.parquet"
    scores.parent.mkdir()
    pd.DataFrame(
        {
            "input_sdf_index": [0, 1, 2, 3],
            "compound_id": ["c0", "c1", "c2", "c3"],
            "engine_status": ["SUCCESS"] * 4,
            "raw_score": [-4.0, -3.0, -2.0, -1.0],
            "standardized_smiles": ["CC", "CCC", "CCCC", "CCCCC"],
        }
    ).to_parquet(scores, index=False)
    manager = JobManager(tmp_path)
    request = JobSubmissionRequest(
        request_id="recall-pool-plan",
        project_id="egfr-c797",
        route="library-first",
        engine="carsidock-pool-selection",
        parameters={
            "carsidock_scores": "inputs/carsidock_scores.parquet",
            "top_n": 3,
        },
        resource_budget=ResourceBudget(
            max_runtime_minutes=10,
            cpu_count=2,
            memory_gb=8,
            max_candidates=3,
        ),
    )
    spec = manager.build_spec(request)
    assert spec.engine == "carsidock-pool-selection"
    assert spec.resource_budget.gpu_count == 0
    assert spec.parameters["input_row_count"] == 4
    assert spec.provenance["algorithm_version"] == "carsidock-recall-pool-v1.0"
    assert "select_carsidock_pool.py" in " ".join(spec.command)
    ligand_spec = manager.build_spec(
        request.model_copy(update={"route": "ligand-first"})
    )
    assert ligand_spec.route == "ligand-first"
    with pytest.raises(ValueError, match="all three"):
        manager.build_spec(
            request.model_copy(
                update={
                    "parameters": {
                        **request.parameters,
                        "score_union_n": 2,
                    }
                }
            )
        )


def test_af3_batch_job_is_registered_gpu_bounded_and_resumable(tmp_path: Path):
    import pandas as pd

    candidate = tmp_path / "inputs" / "stage3.parquet"
    candidate.parent.mkdir()
    pd.DataFrame(
        {
            "input_sdf_index": [0, 1, 2],
            "compound_id": ["c0", "c1", "c2"],
            "standardized_smiles": ["C=CC(=O)NC", "C=CC(=O)NCC", "C=CC(=O)NCCC"],
        }
    ).to_parquet(candidate, index=False)
    source = tmp_path / "inputs" / "protein_seed.json"
    source.write_text(json.dumps({'name': 'seed', 'sequences': [
        {'protein': {'id': 'A', 'sequence': 'A'*104+'C'}}]}))
    target = tmp_path / "configs" / "targets" / "EGFR.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(
        "target_id: EGFR\nprotein_chain: A\ncysteine_sequence_index: 105\n"
        "residue_number: 797\nnucleophilic_atom: SG\n"
    )
    runner = tmp_path / "af3" / "run_alphafold.py"
    python = tmp_path / "af3-env" / "bin" / "python"
    models = tmp_path / "af3" / "models"
    databases = tmp_path / "af3" / "databases"
    runner.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    models.mkdir()
    databases.mkdir()
    runner.write_text("pass\n")
    python.write_text("fake\n")
    (models / "model.bin").write_bytes(b"weights")
    manager = JobManager(tmp_path)
    manager.config = {
        "alphafold3": {
            "runner": str(runner),
            "python": str(python),
            "model_dir": str(models),
            "database_dir": str(databases),
        },
        "runtime": {"random_seed": 17},
    }
    request = JobSubmissionRequest(
        request_id="af3-plan",
        project_id="egfr-c797",
        route="library-first",
        engine="alphafold3-mpae",
        parameters={
            "candidate_table": "inputs/stage3.parquet",
            "source_data_json": "inputs/protein_seed.json",
            "target_config": "configs/targets/EGFR.yaml",
            "top_n": 2,
            "cuda_device_index": 0,
        },
        resource_budget=ResourceBudget(
            max_runtime_minutes=60,
            gpu_count=1,
            cpu_count=4,
            memory_gb=32,
            max_candidates=3,
        ),
    )
    spec = manager.build_spec(request)
    assert spec.parameters["input_row_count"] == 3
    assert spec.parameters["resume"] is True
    assert spec.parameters["seed"] == 17
    assert spec.provenance["algorithm_version"] == "af3-mpae-selection-v1.0"
    assert "run_af3_registered_batch.py" in " ".join(spec.command)
    assert "--resume" in spec.command
    with pytest.raises(ValueError, match="gpu_count"):
        manager.build_spec(
            request.model_copy(
                update={
                    "resource_budget": request.resource_budget.model_copy(
                        update={"gpu_count": 0}
                    )
                }
            )
        )


def test_covdock_lo_job_is_registered_cpu_bounded_and_license_redacted(tmp_path: Path, monkeypatch):
    import pandas as pd

    candidate = tmp_path / "inputs" / "stage4.parquet"
    candidate.parent.mkdir()
    pd.DataFrame(
        {
            "input_sdf_index": [0, 1, 2],
            "compound_id": ["c0", "c1", "c2"],
            "standardized_smiles": ["C=CC(=O)NC", "C=CC(=O)NCC", "C=CC(=O)NCCC"],
            "af3_mpae_rank": [1, 2, 3],
            "covdock_reaction_type": [
                "Michael Addition",
                "Nucleophilic Substitution",
                "Conjugate Addition to Alkyne (carbonyl activated)",
            ],
        }
    ).to_parquet(candidate, index=False)
    receptor = tmp_path / "inputs" / "receptor.pdb"
    write_receptor(receptor)
    root = tmp_path / "schrodinger2021-2"
    root.mkdir()
    (root / "covalent_docking").write_text("fake")
    (root / "run").write_text("fake")
    license_file = tmp_path / "licenses" / "vendor.lic"
    license_file.parent.mkdir()
    license_file.write_text("private-license-material")
    runtime_lib = tmp_path / "runtime" / "lib"
    runtime_lib.mkdir(parents=True)
    manager = JobManager(tmp_path)
    manager.config = {
        "schrodinger": {
            "enabled": True,
            "root": str(root),
            "license_file": str(license_file),
            "runtime_lib_dir": str(runtime_lib),
        }
    }
    request = JobSubmissionRequest(
        request_id="covdock-lo-plan",
        project_id="egfr-c797",
        route="library-first",
        engine="schrodinger-covdock-lo",
        parameters={
            "candidate_table": "inputs/stage4.parquet",
            "receptor": "inputs/receptor.pdb",
            "attachment_residue": "A:797",
            "reaction_type": "Michael Addition",
            "final_n": 2,
            "njobs": 3,
        },
        resource_budget=ResourceBudget(
            max_runtime_minutes=60,
            cpu_count=3,
            memory_gb=16,
            max_candidates=3,
        ),
    )
    spec = manager.build_spec(request)
    assert spec.resource_budget.gpu_count == 0
    assert spec.parameters["input_row_count"] == 3
    assert spec.parameters["host"] == "localhost:3"
    assert spec.parameters["resume"] is True
    assert "run_covdock_lo_registered.py" in " ".join(spec.command)
    assert "--resume" in spec.command
    assert spec.provenance["algorithm_version"] == "covdock-lo-registered-v1.2"
    assert spec.provenance["license_file_sha256"]
    assert "private-license-material" not in json.dumps(spec.provenance)
    environment = _engine_environment(spec, tmp_path)
    assert environment["SCHRODINGER"] == str(root)
    assert environment["SCHROD_LICENSE_FILE"] == str(license_file)
    ligand_spec = manager.build_spec(
        request.model_copy(update={"route": "ligand-first"})
    )
    assert ligand_spec.route == "ligand-first"
    assert ligand_spec.engine == "schrodinger-covdock-lo"
    grouped = manager.build_spec(
        request.model_copy(
            update={
                "route": "ligand-first",
                "parameters": {
                    key: value
                    for key, value in request.parameters.items()
                    if key != "reaction_type"
                }
                | {"reaction_type_column": "covdock_reaction_type"},
            }
        )
    )
    assert "run_ligand_first_covdock_grouped.py" in " ".join(grouped.command)
    assert grouped.parameters["reaction_type"] == "per-candidate-column"
    assert grouped.parameters["checkpoint_size"] == 0
    checkpointed = manager.build_spec(
        request.model_copy(
            update={
                "route": "ligand-first",
                "parameters": {
                    key: value
                    for key, value in request.parameters.items()
                    if key != "reaction_type"
                }
                | {
                    "reaction_type_column": "covdock_reaction_type",
                    "checkpoint_size": 1,
                },
            }
        )
    )
    assert checkpointed.parameters["checkpoint_size"] == 1
    assert checkpointed.parameters["checkpoint_workers"] == 1
    assert checkpointed.parameters["checkpoint_worker_cpus"] == 1
    assert "--checkpoint-size 1" in " ".join(checkpointed.command)
    assert "--checkpoint-workers 1" in " ".join(checkpointed.command)
    sharded = manager.build_spec(
        request.model_copy(
            update={
                "route": "ligand-first",
                "parameters": checkpointed.parameters
                | {"checkpoint_workers": 3, "checkpoint_worker_cpus": 4, "njobs": 1},
                "resource_budget": ResourceBudget(
                    max_runtime_minutes=180,
                    cpu_count=12,
                    memory_gb=48,
                    max_candidates=3,
                ),
            }
        )
    )
    assert sharded.parameters["checkpoint_workers"] == 3
    assert "--checkpoint-workers 3" in " ".join(sharded.command)
    with pytest.raises(ValueError, match="exceeds the approved CPU budget"):
        manager.build_spec(
            request.model_copy(
                update={
                    "route": "ligand-first",
                    "parameters": checkpointed.parameters
                    | {"checkpoint_workers": 3, "checkpoint_worker_cpus": 4},
                }
            )
        )
    with pytest.raises(ValueError, match="require a reaction_type_column"):
        manager.build_spec(
            request.model_copy(
                update={"parameters": request.parameters | {"checkpoint_size": 1}}
            )
        )
    manager.config["runtime"] = {"worker_backend": "external-hpc"}
    original_popen = subprocess.Popen
    def forbid_local_launch(*args, **kwargs):
        if args and args[0][0] == "git":
            return original_popen(*args, **kwargs)
        raise AssertionError("External dispatch must not start a Notebook worker")
    monkeypatch.setattr(subprocess, "Popen", forbid_local_launch)
    execution_request = request.model_copy(update={
        "execution_mode": "execute", "human_approved": True,
        "approval_note": "Approved isolated external queue test only.",
    })
    # The generic HPC adapter never supported Library-first. Reject that
    # combination before registration, then test approval on its supported route.
    with pytest.raises(ValueError, match='bounded CPU HPC boundary'):
        manager.submit(execution_request)
    execution_request = execution_request.model_copy(update={'route': 'ligand-first'})
    # The external queue now requires a native envelope. Boolean approval alone
    # must fail; use an isolated signed fixture, never the production key.
    with pytest.raises(ValueError, match='verified native approval'):
        manager.submit(execution_request)
    from covscreen.native_approval import CURRENT, signed
    from covscreen.approval_scope import normalize_spec
    key = tmp_path / 'test-native.key'
    key.write_bytes(b'isolated-job-test-key-not-production-123456')
    key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(key))
    approved_spec = manager.build_spec(execution_request)
    approved_spec.provenance['worker_backend'] = 'external-hpc'
    token = CURRENT.set(signed({'scope': {'kind': 'job', 'spec': normalize_spec(approved_spec)}}))
    try:
        queued = manager.submit(execution_request)
    finally:
        CURRENT.reset(token)
    assert queued.state == "QUEUED"
    assert queued.pid is None and queued.started_at is None
    assert "no compute launched" in queued.message
    assert manager.registry.load_spec(queued.run_id).provenance["worker_backend"] == "external-hpc"
    with pytest.raises(ValueError, match="scheduler handle"):
        manager.cancel(queued.run_id)
    with pytest.raises(ValueError, match="CPU budget"):
        manager.build_spec(
            request.model_copy(
                update={"parameters": {**request.parameters, "njobs": 4}}
            )
        )


def test_cpu_postprocessing_does_not_inherit_covdock_hpc_backend(tmp_path: Path):
    manager = JobManager(tmp_path)
    manager.config["runtime"] = {"worker_backend": "external-hpc"}
    base = JobSubmissionRequest(
        request_id="postprocess-backend",
        project_id="egfr-c797",
        route="ligand-first",
        engine="ligand-first-evaluation",
        parameters={},
        resource_budget=ResourceBudget(
            max_runtime_minutes=15,
            cpu_count=4,
            memory_gb=16,
            max_candidates=3,
        ),
    )
    assert manager.worker_backend(base) == "local"
    assert manager.worker_backend(
        base.model_copy(update={"engine": "ligand-first-pareto"})
    ) == "local"
    manager.config["runtime"]["postprocessing_worker_backend"] = "external-inspire"
    assert manager.worker_backend(base) == "external-inspire"


def test_library_first_pareto_job_is_registered_without_weighted_sum(tmp_path: Path):
    import pandas as pd

    table = tmp_path / "inputs" / "stage5.parquet"
    table.parent.mkdir()
    pd.DataFrame(
        {
            "input_sdf_index": [0, 1],
            "compound_id": ["c0", "c1"],
            "standardized_smiles": ["C=CC(=O)NC", "C=CC(=O)NCC"],
            "covdock_gate": ["A", "B"],
            "af3_mpae": [1.0, 2.0],
            "property_fit_score": [0.7, 0.8],
            "high_reactivity_alert": [False, False],
            "aggregation_risk": [False, False],
            "pains_alert": ["", ""],
            "admet_score": [0.8, 0.7],
        }
    ).to_parquet(table, index=False)
    trace_config = tmp_path / "configs" / "ligand_first" / "test.yaml"
    trace_config.parent.mkdir(parents=True)
    trace_config.write_text("schema_version: '1.0'\n")
    manager = JobManager(tmp_path)
    request = JobSubmissionRequest(
        request_id="pareto-plan",
        project_id="egfr-c797",
        route="library-first",
        engine="library-first-pareto",
        parameters={
            "candidate_table": "inputs/stage5.parquet",
            "final_n": 2,
            "additional_objectives": {"admet_score": "max"},
        },
        resource_budget=ResourceBudget(
            max_runtime_minutes=10,
            cpu_count=2,
            memory_gb=8,
            max_candidates=2,
        ),
    )
    spec = manager.build_spec(request)
    assert spec.parameters["input_row_count"] == 2
    assert "select_library_first_pareto.py" in " ".join(spec.command)
    assert "admet_score:max" in spec.command
    assert spec.provenance["weighted_sum_used"] is False
    assert spec.provenance["algorithm_version"] == "library-first-pareto-v2.0"
    assert spec.provenance["gate_labels_are_objectives"] is False
    with pytest.raises(ValueError, match="library-first route"):
        manager.build_spec(request.model_copy(update={"route": "ligand-first"}))
    ligand_request = request.model_copy(
        update={
            "route": "ligand-first",
            "engine": "ligand-first-pareto",
            "parameters": {
                **request.parameters,
                "trace_parent_run_id": "test-evaluation-parent-0001",
                "trace_chain_config": "configs/ligand_first/test.yaml",
            },
        }
    )
    ligand_spec = manager.build_spec(ligand_request)
    assert ligand_spec.route == "ligand-first"
    assert ligand_spec.provenance["applied_route"] == "ligand-first"
    assert ligand_spec.provenance["algorithm_version"] == "ligand-first-pareto-v1.0"
    with pytest.raises(ValueError, match="ligand-first route"):
        manager.build_spec(ligand_request.model_copy(update={"route": "de-novo"}))
    with pytest.raises(ValueError, match="library-first route"):
        manager.build_spec(request.model_copy(update={"route": "de-novo"}))


def test_generator_job_specs_are_route_gpu_and_candidate_bounded(tmp_path: Path):
    manager = JobManager(tmp_path)
    generators: dict[str, dict[str, str | bool]] = {}
    for name in ("pocketxmol", "reinvent4", "difflinker"):
        repo = tmp_path / "external" / name
        python = tmp_path / "envs" / name / "bin" / "python"
        checkpoint = repo / "model.ckpt"
        repo.mkdir(parents=True)
        python.parent.mkdir(parents=True)
        python.write_text("fake python")
        checkpoint.write_text(f"{name} checkpoint")
        generators[name] = {
            "enabled": True,
            "repo": str(repo),
            "python": str(python),
            "checkpoint": str(checkpoint),
        }
    (Path(generators["pocketxmol"]["repo"]) / "scripts").mkdir()
    (Path(generators["pocketxmol"]["repo"]) / "scripts" / "sample_use.py").write_text("pass")
    (Path(generators["reinvent4"]["python"]).parent / "reinvent").write_text("fake")
    (Path(generators["difflinker"]["repo"]) / "generate_with_protein.py").write_text("pass")
    manager.config = {"de_novo": generators, "runtime": {"random_seed": 7}}

    protein = tmp_path / "inputs" / "protein.pdb"
    fragments = tmp_path / "inputs" / "fragments.sdf"
    protein.parent.mkdir()
    protein.write_text("END\n")
    fragments.write_text("$$$$\n")
    gpu_budget = ResourceBudget(
        max_runtime_minutes=10, gpu_count=1, cpu_count=2, memory_gb=8, max_candidates=3
    )
    requests = [
        JobSubmissionRequest(
            request_id="pocketxmol-plan",
            project_id="test",
            route="de-novo",
            engine="pocketxmol",
            parameters={
                "protein_pdb": "inputs/protein.pdb",
                "pocket_center": [1.0, 2.0, 3.0],
                "n_candidates": 2,
            },
            resource_budget=gpu_budget,
        ),
        JobSubmissionRequest(
            request_id="reinvent4-plan",
            project_id="test",
            route="de-novo",
            engine="reinvent4",
            parameters={"n_candidates": 3},
            resource_budget=gpu_budget,
        ),
        JobSubmissionRequest(
            request_id="difflinker-plan",
            project_id="test",
            route="ligand-first",
            engine="difflinker",
            parameters={
                "protein_pdb": "inputs/protein.pdb",
                "fragments_sdf": "inputs/fragments.sdf",
                "linker_size": "4,8",
                "n_candidates": 2,
            },
            resource_budget=gpu_budget,
        ),
    ]
    for request in requests:
        result = manager.submit(request)
        spec = manager.registry.load_spec(result.run_id)
        assert result.state == "PLANNED"
        assert spec.parameters["seed"] == 7
        assert spec.provenance["checkpoint_sha256"]
        assert spec.provenance["proposal_scope"].startswith("generation_only")
        assert spec.command[0] == sys.executable

    with pytest.raises(ValueError, match="not registered for route"):
        manager.submit(
            requests[1].model_copy(update={"route": "ligand-first"})
        )
    with pytest.raises(ValueError, match="max_candidates"):
        manager.submit(
            requests[1].model_copy(
                update={"parameters": {"n_candidates": 4}}
            )
        )
    with pytest.raises(ValueError, match="gpu_count"):
        manager.submit(
            requests[1].model_copy(
                update={
                    "resource_budget": ResourceBudget(
                        max_runtime_minutes=10, gpu_count=0, max_candidates=3
                    )
                }
            )
        )
    cpu_request = requests[1].model_copy(
        update={
            "parameters": {"n_candidates": 1, "device": "cpu"},
            "resource_budget": ResourceBudget(
                max_runtime_minutes=10, gpu_count=0, max_candidates=1
            ),
        }
    )
    cpu_spec = manager.build_spec(cpu_request)
    assert cpu_spec.parameters["device"] == "cpu"
    assert cpu_spec.resource_budget.gpu_count == 0


def test_candidate_reader_returns_json_safe_nulls(tmp_path: Path):
    manager = JobManager(tmp_path)
    result = manager.submit(rdkit_request())
    table = manager.registry.job_dir(result.run_id) / "artifacts" / "scores.csv"
    table.parent.mkdir(parents=True, exist_ok=True)
    with table.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["compound_id", "score"])
        writer.writerow(["c1", ""])
    result.candidate_table_path = str(table.relative_to(tmp_path))
    manager.registry.write_result(result)
    payload = get_job_candidates_tool(tmp_path, result.run_id)
    assert payload["candidates"] == [{"compound_id": "c1", "score": None}]


def test_library_first_evaluation_preserves_denominator(tmp_path: Path):
    source = tmp_path / "scores.csv"
    with source.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "compound_id", "input_sdf_index", "label", "raw_score",
                "engine_status", "failure_reason",
            ],
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "compound_id": "active-1", "input_sdf_index": 1,
                    "label": "active", "raw_score": 0.9,
                    "engine_status": "SUCCESS", "failure_reason": "",
                },
                {
                    "compound_id": "decoy-1", "input_sdf_index": 2,
                    "label": "decoy", "raw_score": 0.1,
                    "engine_status": "SUCCESS", "failure_reason": "",
                },
                {
                    "compound_id": "decoy-2", "input_sdf_index": 3,
                    "label": "decoy", "raw_score": "",
                    "engine_status": "FAILED", "failure_reason": "upstream_failure",
                },
            ]
        )
    output_dir = tmp_path / "evaluation"
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "run_library_first_evaluation.py"),
            "--score-table", str(source),
            "--engine-name", "test-engine",
            "--score-direction", "higher_is_better",
            "--output-dir", str(output_dir),
            "--expected-rows", "3",
            "--expected-active", "1",
            "--expected-decoy", "2",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stderr == ""
    metrics = json.loads((output_dir / "evaluation_metrics.json").read_text())
    assert metrics["n_input"] == 3
    assert metrics["n_scoring_success"] == 2
    assert metrics["n_failed"] == 1
    evaluated = list(csv.DictReader((output_dir / "evaluated_candidates.csv").open()))
    assert len(evaluated) == 3
    assert evaluated[-1]["compound_id"] == "decoy-2"


def test_library_first_evaluation_accepts_categorical_parquet(tmp_path: Path):
    import pandas as pd

    source = tmp_path / "scores.parquet"
    pd.DataFrame(
        {
            "compound_id": ["active-1", "decoy-1"],
            "input_sdf_index": [1, 2],
            "label": pd.Categorical(["active", "decoy"]),
            "raw_score": [0.9, 0.1],
            "engine_status": ["SUCCESS", "SUCCESS"],
        }
    ).to_parquet(source, index=False)
    output_dir = tmp_path / "evaluation"
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "scripts" / "run_library_first_evaluation.py"),
            "--score-table", str(source),
            "--engine-name", "test-engine",
            "--score-direction", "higher_is_better",
            "--output-dir", str(output_dir),
            "--expected-rows", "2",
            "--expected-active", "1",
            "--expected-decoy", "1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (output_dir / "evaluation_metrics.json").is_file()
