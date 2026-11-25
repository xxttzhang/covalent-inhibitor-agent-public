from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from covscreen.chain_contract import ChainStatus, StageStatus

from pydantic import BaseModel, ConfigDict, Field

from covscreen.jobs import ArtifactRegistry, JobSubmissionRequest, ResourceBudget, RunResult, sha256_file
from covscreen.layout import resolve_library_config


StageKey = Literal[
    "prescreen",
    "carsidock",
    "rtmscore",
    "recall_pool",
    "af3",
    "covdock_lo",
    "pareto",
]

STAGE_ENGINES: dict[StageKey, str] = {
    "prescreen": "covindb-prescreen",
    "carsidock": "carsidock-cov",
    "rtmscore": "rtmscore",
    "recall_pool": "carsidock-pool-selection",
    "af3": "alphafold3-mpae",
    "covdock_lo": "schrodinger-covdock-lo",
    "pareto": "library-first-pareto",
}
STAGE_ORDER: tuple[StageKey, ...] = tuple(STAGE_ENGINES)  # type: ignore[assignment]


class LibraryFirstChainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    campaign_id: str
    project_id: str
    terminal_stage: StageKey = "pareto"
    target_config: str
    pdb: str
    reference_ligand: str
    af3_source_data_json: str | None = None
    covdock_receptor: str | None = None
    docking_binding: str | None = None
    attachment_residue: str
    reaction_type: str = "Michael Addition"
    covres: str
    seed_run_ids: dict[StageKey, str] = Field(default_factory=dict)
    prescreen_top_n: int = Field(default=20_000, ge=1)
    prescreen_max_rows: int | None = Field(default=None, ge=1)
    recall_pool_n: int = Field(default=2_000, ge=1)
    af3_top_n: int = Field(default=100, ge=1)
    covdock_top_n: int = Field(default=100, ge=1)
    final_n: int = Field(default=30, ge=1)
    docking_max_runtime_minutes: int = Field(default=2880, ge=1, le=2880)
    scoring_max_runtime_minutes: int = Field(default=720, ge=1, le=720)
    seed: int = 20260725
    cuda_device_index: int = Field(default=0, ge=0)
    min_carsidock_success_rate: float = Field(default=0.75, ge=0, le=1)
    min_af3_coverage: float = Field(default=0.95, ge=0, le=1)
    min_covdock_success_rate: float = Field(default=0.80, ge=0, le=1)
    covdock_cpu_count: int = Field(default=55, ge=1, le=128)


class ChainStageStatus(StageStatus[StageKey]):
    pass


class LibraryFirstChainStatus(ChainStatus[StageKey]):
    stages: list[ChainStageStatus]


def load_chain_config(path: Path) -> LibraryFirstChainConfig:
    import yaml

    return LibraryFirstChainConfig.model_validate(yaml.safe_load(path.read_text()) or {})


def _project_path(root: Path, value: str, *, must_exist: bool = True) -> Path:
    path = Path(value)
    path = path.resolve() if path.is_absolute() else (root / path).resolve()
    if must_exist and path.suffix in {'.yaml', '.yml'}:
        path = resolve_library_config(root, value)
    else:
        path.relative_to(root)
    if must_exist and not path.exists():
        raise ValueError(f"chain input does not exist: {value}")
    return path


def _manifest(root: Path, run_id: str, filename: str) -> dict[str, Any] | None:
    path = root / "state" / "jobs" / run_id / "artifacts" / filename
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def _manifest_gates(payload: dict[str, Any], key: str = "quality_gates") -> bool:
    gates = payload.get(key)
    return isinstance(gates, dict) and bool(gates) and all(value is True for value in gates.values())


def _completed_quality(root: Path, config: LibraryFirstChainConfig, stage: StageKey, run_id: str) -> tuple[bool, str]:
    artifacts = root / "state" / "jobs" / run_id / "artifacts"
    if stage in {"prescreen", "carsidock", "rtmscore"}:
        from covscreen.result_verification import verify_job
        try:
            verification = verify_job(root, run_id)
        except (OSError, ValueError, KeyError) as exc:
            return False, "prescreen verification failed: " + str(exc)
        if not verification['verified']:
            return False, "; ".join(verification['errors'])
        if stage == "prescreen":
            return True, "prescreen inputs, identities and outputs verified"
    if stage == "carsidock":
        import pandas as pd

        path = artifacts / "carsidock_scores.parquet"
        if not path.is_file():
            return False, "carsidock_scores.parquet is missing"
        frame = pd.read_parquet(path, columns=["engine_status"])
        success = int(frame["engine_status"].eq("SUCCESS").sum())
        rate = success / len(frame) if len(frame) else 0.0
        audit = artifacts / "carsidock_scores_audit.json"
        passed = audit.is_file() and rate >= config.min_carsidock_success_rate
        return passed, f"CarsiDock success rate={rate:.3f}; threshold={config.min_carsidock_success_rate:.3f}"
    if stage == "rtmscore":
        payload = _manifest(root, run_id, "rtmscore.run_manifest.json")
        passed = bool(
            payload
            and payload.get("status") == "SUCCESS"
            and payload.get("output_row_count", 0) > 0
            and payload.get("output_row_count")
            == payload.get("scored_row_count", 0) + payload.get("failed_row_count", 0)
        )
        return passed, "RTMScore identity denominator is complete" if passed else "RTMScore manifest or identity denominator failed"
    if stage == "recall_pool":
        payload = _manifest(root, run_id, "stage3_selection_manifest.json")
        passed = bool(payload and payload.get("status") == "completed" and _manifest_gates(payload))
        return passed, "recall-pool manifest and quality gates PASS" if passed else "recall-pool quality manifest/gates missing or failed"
    if stage == "af3":
        payload = _manifest(root, run_id, "stage4_selection_manifest.json")
        passed = bool(payload and payload.get("status") == "completed" and _manifest_gates(payload))
        return passed, "AF3 coverage and identity gates PASS" if passed else "AF3 quality manifest/gates missing or failed"
    if stage == "covdock_lo":
        payload = _manifest(root, run_id, "covdock_lo_registered_manifest.json")
        gates = payload.get("gate_quality_gates") if payload else None
        passed = bool(payload and payload.get("status") == "completed" and isinstance(gates, dict) and gates and all(value is True for value in gates.values()))
        return passed, "CovDock-LO pose gates PASS" if passed else "CovDock-LO manifest/pose gates missing or failed"
    payload = _manifest(root, run_id, "stage6_pareto_manifest.json")
    passed = bool(payload and payload.get("status") == "completed" and _manifest_gates(payload))
    return passed, "Pareto baseline evidence and selection gates PASS" if passed else "Pareto manifest/evidence gates missing or failed"


def _result_index(registry: ArtifactRegistry, project_id: str) -> dict[str, list[RunResult]]:
    indexed: dict[str, list[RunResult]] = {}
    for result in registry.list_results(limit=10_000):
        if result.project_id != project_id:
            continue
        indexed.setdefault(result.engine, []).append(result)
    return indexed


def _recoverable_carsidock_prefix(root: Path, run_id: str) -> Path | None:
    prefix = root / "state" / "jobs" / run_id / "artifacts" / "carsidock"
    required = (
        prefix.with_suffix(".sdf"),
        Path(f"{prefix}_nodumat.sdf"),
        Path(f"{prefix}_withpckt.pdb"),
    )
    return prefix if all(path.is_file() for path in required) else None


def _recoverable_af3_artifacts(root: Path, run_id: str) -> Path | None:
    artifacts = root / "state" / "jobs" / run_id / "artifacts"
    progress_path = artifacts / "progress.json"
    batch_manifest = artifacts / "prepared" / "batch_manifest.json"
    predictions = artifacts / "predictions"
    if not progress_path.is_file() or not batch_manifest.is_file() or not predictions.is_dir():
        return None
    try:
        progress = json.loads(progress_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    total = int(progress.get("total", 0))
    completed = int(progress.get("completed", 0))
    failed = int(progress.get("failed", 0))
    if total < 1 or completed != total or failed != 0:
        return None
    return artifacts


def inspect_chain(root: Path, config: LibraryFirstChainConfig) -> LibraryFirstChainStatus:
    root = root.resolve()
    registry = ArtifactRegistry(root, create=False)
    indexed = _result_index(registry, config.project_id)
    statuses: list[ChainStageStatus] = []
    parent: str | None = None
    terminal: Literal["ready", "active", "quality_gate_failed", "dependency_quarantined", "completed"] = "ready"
    next_stage: StageKey | None = None

    for stage in STAGE_ORDER[:STAGE_ORDER.index(config.terminal_stage) + 1]:
        engine = STAGE_ENGINES[stage]
        seed = config.seed_run_ids.get(stage)
        candidates = indexed.get(engine, [])
        if seed:
            candidates = [item for item in candidates if item.run_id == seed]
        elif parent:
            candidates = [item for item in candidates if item.parent_run_id == parent]
        candidates.sort(key=lambda item: item.submitted_at, reverse=True)
        completed = next((item for item in candidates if item.state == "COMPLETED"), None)
        selected = (completed
            or next((item for item in candidates if item.state in {"QUEUED", "RUNNING", "PLANNED"}), None)
            or next((item for item in candidates if item.state in {"FAILED", "CANCELLED", "TIMED_OUT"}), None))
        if selected is not None:
            from covscreen.branch_guard import BranchStopped, require_unquarantined
            try:
                require_unquarantined(root, registry.load_spec(selected.run_id))
            except BranchStopped as error:
                statuses.append(ChainStageStatus(stage=stage, engine=engine, state=selected.state,
                    run_id=selected.run_id, parent_run_id=selected.parent_run_id,
                    quality_pass=None, reason=str(error)))
                terminal = "dependency_quarantined"
                break
        if completed is not None:
            quality, reason = _completed_quality(root, config, stage, completed.run_id)
            statuses.append(
                ChainStageStatus(
                    stage=stage,
                    engine=engine,
                    state=completed.state,
                    run_id=completed.run_id,
                    parent_run_id=completed.parent_run_id,
                    quality_pass=quality,
                    reason=reason,
                )
            )
            if not quality:
                terminal = "quality_gate_failed"
                break
            parent = completed.run_id
            continue
        active = next((item for item in candidates if item.state in {"QUEUED", "RUNNING", "PLANNED"}), None)
        if active is not None:
            statuses.append(
                ChainStageStatus(
                    stage=stage,
                    engine=engine,
                    state=active.state,
                    run_id=active.run_id,
                    parent_run_id=active.parent_run_id,
                    quality_pass=None,
                    reason="registered stage is not yet completed",
                )
            )
            terminal = "active"
            break
        failed = next(
            (
                item
                for item in candidates
                if item.state in {"FAILED", "CANCELLED", "TIMED_OUT"}
            ),
            None,
        )
        if failed is not None:
            recoverable = bool(
                (stage == "carsidock" and _recoverable_carsidock_prefix(root, failed.run_id))
                or (stage == "af3" and _recoverable_af3_artifacts(root, failed.run_id))
            )
            statuses.append(
                ChainStageStatus(
                    stage=stage,
                    engine=engine,
                    state=failed.state,
                    run_id=failed.run_id,
                    parent_run_id=failed.parent_run_id,
                    quality_pass=None,
                    reason=(
                        f"failed {engine} run retained complete reusable artifacts; "
                        "a registered CPU-only recovery can be compiled"
                        if recoverable
                        else "latest registered attempt failed; a new attempt can be compiled"
                    ),
                )
            )
            next_stage = stage
            terminal = "ready"
            break
        statuses.append(
            ChainStageStatus(
                stage=stage,
                engine=engine,
                state="NOT_SUBMITTED",
                parent_run_id=parent,
                quality_pass=None,
                reason="all upstream stages passed; request can be compiled",
            )
        )
        next_stage = stage
        terminal = "ready"
        break
    else:
        terminal = "completed"

    return LibraryFirstChainStatus(
        campaign_id=config.campaign_id,
        project_id=config.project_id,
        stages=statuses,
        next_stage=next_stage,
        next_parent_run_id=parent,
        terminal_state=terminal,
    )


def _artifact(root: Path, run_id: str, filename: str) -> str:
    path = root / "state" / "jobs" / run_id / "artifacts" / filename
    if not path.is_file():
        raise ValueError(f"required parent artifact is missing: {path}")
    return str(path.relative_to(root))


def _row_count(path: Path) -> int:
    import pandas as pd

    if path.suffix.lower() == ".parquet":
        return len(pd.read_parquet(path, columns=["input_sdf_index"]))
    return len(pd.read_csv(path, usecols=["input_sdf_index"]))


def compile_next_request(
    root: Path,
    config: LibraryFirstChainConfig,
    status: LibraryFirstChainStatus,
    *,
    execution_mode: Literal["plan", "execute"] = "plan",
    human_approved: bool = False,
    approval_note: str | None = None,
) -> JobSubmissionRequest | None:
    root = root.resolve()
    stage = status.next_stage
    if stage is None:
        return None
    parent = status.next_parent_run_id
    parameters: dict[str, Any]
    budget: ResourceBudget

    if stage == "prescreen":
        parameters = {
            "target_config": config.target_config,
            "top_n": config.prescreen_top_n,
            "max_rows": config.prescreen_max_rows,
            "chunk_size": 5_000,
            "seed": config.seed,
        }
        budget = ResourceBudget(max_runtime_minutes=720, cpu_count=8, memory_gb=64, max_candidates=config.prescreen_top_n)
    elif stage == "carsidock":
        assert parent is not None
        table = _artifact(root, parent, "stage2_candidates.parquet")
        sdf = _artifact(root, parent, "stage2_candidates.sdf")
        count = _row_count(root / table)
        parameters = {
            "pdb": config.pdb,
            "reference_ligand": config.reference_ligand,
            "ligand_sdf": sdf,
            "ligand_table": table,
            "covres": config.covres,
            "chunk": 0,
            "device": "cuda",
            "num_conformer": 1,
            "cuda_device_index": config.cuda_device_index,
            "seed": config.seed,
        }
        if config.docking_binding:
            parameters["docking_binding"] = config.docking_binding
        failed_stage = next(
            (
                item
                for item in status.stages
                if item.stage == "carsidock"
                and item.state in {"FAILED", "CANCELLED", "TIMED_OUT"}
                and item.run_id
            ),
            None,
        )
        recoverable_prefix = (
            _recoverable_carsidock_prefix(root, failed_stage.run_id)
            if failed_stage and failed_stage.run_id
            else None
        )
        if recoverable_prefix is not None and failed_stage is not None:
            parameters.update(
                {
                    "device": "cpu",
                    "reuse_output_prefix": str(recoverable_prefix.relative_to(root)),
                    "recovery_source_run_id": failed_stage.run_id,
                }
            )
            budget = ResourceBudget(
                max_runtime_minutes=60,
                gpu_count=0,
                cpu_count=4,
                memory_gb=32,
                max_candidates=count,
            )
        else:
            budget = ResourceBudget(max_runtime_minutes=config.docking_max_runtime_minutes, gpu_count=1, cpu_count=4, memory_gb=64, max_candidates=count)
    elif stage == "rtmscore":
        assert parent is not None
        scores = _artifact(root, parent, "carsidock_scores.parquet")
        count = _row_count(root / scores)
        parameters = {
            "pdb": config.pdb,
            "reference_ligand": config.reference_ligand,
            "carsidock_scores": scores,
            "max_ligands": count,
            "cuda_device_index": config.cuda_device_index,
        }
        budget = ResourceBudget(max_runtime_minutes=config.scoring_max_runtime_minutes, gpu_count=1, cpu_count=4, memory_gb=64, max_candidates=count)
    elif stage == "recall_pool":
        assert parent is not None
        rtmscore = _artifact(root, parent, "rtmscore.parquet")
        parent_result = ArtifactRegistry(root, create=False).load_result(parent)
        if not parent_result.parent_run_id:
            raise ValueError("RTMScore result lacks its CarsiDock parent_run_id")
        carsi = _artifact(root, parent_result.parent_run_id, "carsidock_scores.parquet")
        count = _row_count(root / carsi)
        top_n = min(config.recall_pool_n, count)
        union = round(top_n * 0.70)
        diversity = round(top_n * 0.20)
        rescue = top_n - union - diversity
        parameters = {
            "carsidock_scores": carsi,
            "rtmscore_scores": rtmscore,
            "top_n": top_n,
            "score_union_n": union,
            "diversity_n": diversity,
            "rescue_n": rescue,
            "min_carsidock_success_rate": config.min_carsidock_success_rate,
        }
        budget = ResourceBudget(max_runtime_minutes=60, cpu_count=4, memory_gb=32, max_candidates=top_n)
    elif stage == "af3":
        assert parent is not None
        table = _artifact(root, parent, "stage3_recall_pool.parquet")
        count = _row_count(root / table)
        parameters = {
            "candidate_table": table,
            "source_data_json": config.af3_source_data_json,
            "target_config": config.target_config,
            "top_n": min(config.af3_top_n, count),
            "cuda_device_index": config.cuda_device_index,
            "num_recycles": 3,
            "num_diffusion_samples": 1,
            "num_seeds": 1,
            "cpu_per_search": 2,
            "seed": config.seed,
            "min_coverage": config.min_af3_coverage,
            "run_data_pipeline": False,
            "resume": True,
        }
        failed_stage = next(
            (
                item
                for item in status.stages
                if item.stage == "af3"
                and item.state in {"FAILED", "CANCELLED", "TIMED_OUT"}
                and item.run_id
            ),
            None,
        )
        reusable_artifacts = (
            _recoverable_af3_artifacts(root, failed_stage.run_id)
            if failed_stage and failed_stage.run_id
            else None
        )
        if reusable_artifacts is not None and failed_stage is not None:
            parameters.update(
                {
                    "cuda_device_index": 0,
                    "reuse_artifact_dir": str(reusable_artifacts.relative_to(root)),
                    "recovery_source_run_id": failed_stage.run_id,
                }
            )
            budget = ResourceBudget(
                max_runtime_minutes=60,
                gpu_count=0,
                cpu_count=4,
                memory_gb=32,
                max_candidates=count,
            )
        else:
            budget = ResourceBudget(max_runtime_minutes=10_080, gpu_count=1, cpu_count=4, memory_gb=96, max_candidates=count)
    elif stage == "covdock_lo":
        assert parent is not None
        table = _artifact(root, parent, "stage4_top_candidates.parquet")
        count = _row_count(root / table)
        parameters = {
            "candidate_table": table,
            "receptor": config.covdock_receptor,
            "attachment_residue": config.attachment_residue,
            "nucleophilic_atom": "SG",
            "reaction_type": config.reaction_type,
            "final_n": min(config.covdock_top_n, count),
            "njobs": min(config.covdock_cpu_count, count),
            "host": f"localhost:{min(config.covdock_cpu_count, count)}",
            "min_success_rate": config.min_covdock_success_rate,
            "resume": True,
        }
        budget = ResourceBudget(max_runtime_minutes=10_080, cpu_count=config.covdock_cpu_count, memory_gb=128, max_candidates=count)
    else:
        assert parent is not None
        table = _artifact(root, parent, "stage5_gated_candidates.parquet")
        count = _row_count(root / table)
        parameters = {"candidate_table": table, "final_n": min(config.final_n, count)}
        budget = ResourceBudget(max_runtime_minutes=30, cpu_count=2, memory_gb=16, max_candidates=count)

    return JobSubmissionRequest(
        request_id=f"{config.campaign_id}-{stage}-{execution_mode}-v1",
        project_id=config.project_id,
        route="library-first",
        engine=STAGE_ENGINES[stage],
        parameters=parameters,
        resource_budget=budget,
        execution_mode=execution_mode,
        human_approved=human_approved,
        approval_note=approval_note,
        parent_run_id=parent,
    )


def chain_provenance(root: Path, config_path: Path) -> dict[str, Any]:
    return {
        "config_path": str(config_path.resolve().relative_to(root.resolve())) if config_path.resolve().is_relative_to(root.resolve()) else str(resolve_library_config(root, config_path)),
        "config_sha256": sha256_file(config_path.resolve()),
        "stage_order": list(STAGE_ORDER),
        "engines": STAGE_ENGINES,
    }
