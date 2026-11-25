from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from covscreen.chain_contract import ChainStatus, StageStatus

from pydantic import BaseModel, ConfigDict, Field, model_validator

from covscreen.layout import config_reference, resolve_route_config
from covscreen.jobs import ArtifactRegistry, JobSubmissionRequest, ResourceBudget, RunResult, sha256_file


StageKey = Literal[
    "structure_intake",
    "covalentize",
    "carsidock",
    "carsidock_selection",
    "af3",
    "covdock_lo",
    "independent_evaluation",
    "pareto",
]

STAGE_ENGINES: dict[StageKey, str] = {
    "structure_intake": "ligand-first-structure-intake",
    "covalentize": "ligand-first-covalentization",
    "carsidock": "carsidock-cov",
    "carsidock_selection": "carsidock-pool-selection",
    "af3": "alphafold3-mpae",
    "covdock_lo": "schrodinger-covdock-lo",
    "independent_evaluation": "ligand-first-evaluation",
    "pareto": "ligand-first-pareto",
}
STAGE_ORDER: tuple[StageKey, ...] = tuple(STAGE_ENGINES)  # type: ignore[assignment]


class LigandFirstChainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    stage_order: list[StageKey] = Field(default_factory=lambda: list(STAGE_ORDER))
    campaign_id: str
    project_id: str
    campaign_path: str
    trace_chain_config: str
    target_aliases: list[str] = Field(default_factory=list)
    ligand_aliases: list[str] = Field(default_factory=list)
    target_config: str
    pdb: str
    reference_ligand: str
    af3_source_data_json: str
    covdock_receptor: str
    attachment_residue: str
    covres: str
    seed_run_ids: dict[StageKey, str] = Field(default_factory=dict)
    dispatch_policy: Literal["ligand-smoke-v1"] | None = None
    dispatch_batch: str | None = None
    max_sites: int = Field(default=8, ge=1, le=100)
    enumeration_backend: Literal["rdkit-branch", "upstream-native"] = "rdkit-branch"
    upstream_covalentizer_repo: str = "external/Covalentizer"
    upstream_candidate_budget: int = Field(default=512, ge=1, le=10000)
    max_branch_heavy_atoms: int = Field(default=12, ge=1, le=50)
    max_distance_angstrom: float = Field(default=12.0, ge=2.0, le=30.0)
    max_vector_angle_degrees: float = Field(default=100.0, ge=1.0, le=180.0)
    carsidock_top_n: int = Field(default=96, ge=1)
    af3_top_n: int = Field(default=48, ge=1)
    covdock_top_n: int = Field(default=48, ge=1)
    final_n: int = Field(default=30, ge=1)
    seed: int = 20260725
    cuda_device_index: int = Field(default=0, ge=0)
    min_carsidock_success_rate: float = Field(default=0.75, ge=0, le=1)
    min_af3_coverage: float = Field(default=0.95, ge=0, le=1)
    min_covdock_success_rate: float = Field(default=0.80, ge=0, le=1)
    min_parent_mcs_fraction: float = Field(default=0.60, ge=0, le=1)
    covdock_cpu_count: int = Field(default=55, ge=1, le=128)
    covdock_checkpoint_size: int = Field(default=1, ge=0, le=100)

    @model_validator(mode="after")
    def contiguous_stage_scope(self) -> "LigandFirstChainConfig":
        if not self.stage_order or len(set(self.stage_order)) != len(self.stage_order):
            raise ValueError("stage_order must be non-empty and contain no duplicates")
        first = STAGE_ORDER.index(self.stage_order[0])
        expected = list(STAGE_ORDER[first:first + len(self.stage_order)])
        if self.stage_order != expected or self.stage_order[-1] != "pareto":
            raise ValueError(
                "stage_order must be a contiguous suffix of the registered Ligand-first chain ending in pareto"
            )
        unknown_seeds = sorted(set(self.seed_run_ids) - set(self.stage_order))
        if unknown_seeds:
            raise ValueError(f"seed_run_ids contains stages outside stage_order: {unknown_seeds}")
        if (self.dispatch_policy is None) != (self.dispatch_batch is None):
            raise ValueError("dispatch_policy and dispatch_batch must be configured together")
        if self.dispatch_batch is not None:
            import re

            if not re.fullmatch(r"ligand-smoke-[a-z0-9-]{1,64}", self.dispatch_batch):
                raise ValueError("dispatch_batch is outside the bounded Ligand-first smoke namespace")
            executable = [stage for stage in self.stage_order if stage not in self.seed_run_ids]
            if any(stage not in {"carsidock", "af3", "covdock_lo", "independent_evaluation", "pareto"}
                   for stage in executable):
                raise ValueError("ligand-smoke-v1 cannot dispatch unregistered preparation stages")
            if max(self.carsidock_top_n, self.af3_top_n, self.covdock_top_n, self.final_n) > 3:
                raise ValueError("ligand-smoke-v1 is limited to three candidates")
            if self.covdock_cpu_count > 4:
                raise ValueError("ligand-smoke-v1 CovDock is limited to four CPUs")
        return self


class ChainStageStatus(StageStatus[StageKey]):
    pass


class LigandFirstChainStatus(ChainStatus[StageKey]):
    stages: list[ChainStageStatus]


def load_ligand_chain_config(path: Path) -> LigandFirstChainConfig:
    import yaml

    return LigandFirstChainConfig.model_validate(yaml.safe_load(path.read_text()) or {})


def _manifest(root: Path, run_id: str, name: str) -> dict[str, Any] | None:
    path = root / "state" / "jobs" / run_id / "artifacts" / name
    return json.loads(path.read_text()) if path.is_file() else None


def _all_gates(payload: dict[str, Any] | None, key: str = "quality_gates") -> bool:
    gates = payload.get(key) if payload else None
    return bool(isinstance(gates, dict) and gates and all(value is True for value in gates.values()))


def _quality(
    root: Path,
    config: LigandFirstChainConfig,
    stage: StageKey,
    run_id: str,
) -> tuple[bool, str]:
    artifacts = root / "state" / "jobs" / run_id / "artifacts"
    if stage == "covalentize":
        payload = _manifest(root, run_id, "ligand_first_candidates.json")
        if config.enumeration_backend == "upstream-native":
            passed = bool(payload and payload.get("site_discovery_mode") == "upstream-covalentizer-native-v1"
                          and payload.get("status") == "prepared" and _all_gates(payload)
                          and int(payload.get("chemistry_pass_count", 0)) > 0)
            return passed, "native Covalentizer chemistry, parent and budget gates PASS" if passed else "native Covalentizer manifest/gates missing or failed"
        passed = bool(
            payload
            and payload.get("site_discovery_mode") == "covalentizer-rdkit-rule-v1"
            and int(payload.get("advancing_site_count", 0)) > 0
            and int(payload.get("chemistry_pass_count", 0)) > 0
        )
        return passed, "Covalentizer-style site discovery and chemistry gates PASS" if passed else "no advancing site or chemistry-valid candidate"
    if stage == "structure_intake":
        payload = _manifest(root, run_id, "ligand_first_input_audit.json")
        parent_pose = artifacts / "parent_ligand_pose.sdf"
        passed = bool(
            payload
            and payload.get("status") == "PASS"
            and payload.get("asset_checks")
            and all(payload["asset_checks"].values())
            and parent_pose.is_file()
            and parent_pose.stat().st_size > 0
        )
        return passed, "source checksums, residue mapping and parent pose reconstruction PASS" if passed else "structure source, checksum, residue mapping or parent pose audit failed"
    if stage == "carsidock":
        import pandas as pd

        path = artifacts / "carsidock_scores.parquet"
        audit = artifacts / "carsidock_scores_audit.json"
        if not path.is_file() or not audit.is_file():
            return False, "CarsiDock score table or audit is missing"
        frame = pd.read_parquet(path, columns=["engine_status"])
        rate = float(frame["engine_status"].eq("SUCCESS").mean()) if len(frame) else 0.0
        passed = rate >= config.min_carsidock_success_rate
        return passed, f"CarsiDock success rate={rate:.3f}; threshold={config.min_carsidock_success_rate:.3f}"
    if stage == "carsidock_selection":
        payload = _manifest(root, run_id, "stage3_selection_manifest.json")
        passed = bool(payload and payload.get("status") == "completed" and _all_gates(payload))
        return passed, "CarsiScore/diversity selection gates PASS" if passed else "CarsiDock selection manifest/gates missing or failed"
    if stage == "af3":
        payload = _manifest(root, run_id, "stage4_selection_manifest.json")
        passed = bool(payload and payload.get("status") == "completed" and _all_gates(payload))
        return passed, "AF3 coverage and identity gates PASS" if passed else "AF3 quality manifest/gates missing or failed"
    if stage == "covdock_lo":
        payload = _manifest(root, run_id, "covdock_lo_registered_manifest.json")
        passed = bool(payload and payload.get("status") == "completed" and _all_gates(payload, "gate_quality_gates"))
        return passed, "reaction-routed CovDock-LO identity gates PASS" if passed else "CovDock-LO reaction/identity gates missing or failed"
    if stage == "independent_evaluation":
        payload = _manifest(root, run_id, "ligand_first_evaluation_manifest.json")
        passed = bool(payload and payload.get("status") == "completed" and _all_gates(payload))
        return passed, "independent chemistry and physical evaluation gates PASS" if passed else "independent evaluation gates missing or failed"
    payload = _manifest(root, run_id, "stage6_pareto_manifest.json")
    trace_json = artifacts / "ligand_first_traceability_report.json"
    trace_md = artifacts / "ligand_first_traceability_report.md"
    trace: dict[str, Any] | None = None
    if trace_json.is_file():
        try:
            trace = json.loads(trace_json.read_text())
        except (OSError, json.JSONDecodeError):
            trace = None
    expected_engines = [STAGE_ENGINES[item] for item in config.stage_order]
    trace_stages = trace.get("stages", []) if trace else []
    config_path = root / config.trace_chain_config
    config_record = trace.get("chain_config", {}) if trace else {}
    outputs = payload.get("outputs", {}) if payload else {}
    trace_ok = bool(
        trace
        and trace.get("route") == "ligand-first"
        and int(trace.get("stage_count", 0)) >= len(config.stage_order)
        and [item.get("engine") for item in trace_stages][-len(expected_engines):]
        == expected_engines
        and all(item.get("state") == "COMPLETED" for item in trace_stages)
        and trace_stages[-1].get("run_id") == run_id
        and config_path.is_file()
        and config_record.get("path") == config.trace_chain_config
        and config_record.get("sha256") == sha256_file(config_path)
        and trace_md.is_file()
        and outputs.get(trace_json.name, {}).get("sha256") == sha256_file(trace_json)
        and outputs.get(trace_md.name, {}).get("sha256") == sha256_file(trace_md)
    )
    passed = bool(
        payload
        and payload.get("status") == "completed"
        and payload.get("selection_policy", {}).get("route_profile") == "ligand-first"
        and _all_gates(payload)
        and trace_ok
    )
    return passed, "Ligand-first Pareto selection and eight-stage traceability gates PASS" if passed else "Ligand-first Pareto or complete traceability evidence is missing/failed"


def _result_index(registry: ArtifactRegistry, project_id: str) -> dict[str, list[RunResult]]:
    indexed: dict[str, list[RunResult]] = {}
    for result in registry.list_results(limit=10_000):
        if result.project_id == project_id and result.route == "ligand-first":
            indexed.setdefault(result.engine, []).append(result)
    return indexed


def _scientific_parent(registry: ArtifactRegistry, result: RunResult) -> str | None:
    """Return scientific lineage without confusing it with retry ancestry."""
    try:
        spec = registry.load_spec(result.run_id)
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        return result.parent_run_id
    explicit = spec.parameters.get("upstream_run_id")
    return str(explicit) if explicit else result.parent_run_id


def inspect_ligand_chain(root: Path, config: LigandFirstChainConfig) -> LigandFirstChainStatus:
    root = root.resolve()
    registry = ArtifactRegistry(root, create=False)
    indexed = _result_index(registry, config.project_id)
    statuses: list[ChainStageStatus] = []
    parent: str | None = None
    terminal: Literal["ready", "active", "quality_gate_failed", "completed"] = "ready"
    next_stage: StageKey | None = None
    for stage in config.stage_order:
        engine = STAGE_ENGINES[stage]
        seed = config.seed_run_ids.get(stage)
        candidates = indexed.get(engine, [])
        if seed:
            candidates = [item for item in candidates if item.run_id == seed]
        elif parent:
            candidates = [
                item for item in candidates
                if _scientific_parent(registry, item) == parent
            ]
        elif stage != "structure_intake":
            candidates = []
        candidates.sort(key=lambda item: item.submitted_at, reverse=True)
        completed = next((item for item in candidates if item.state == "COMPLETED"), None)
        if completed:
            passed, reason = _quality(root, config, stage, completed.run_id)
            statuses.append(ChainStageStatus(stage=stage, engine=engine, state=completed.state, run_id=completed.run_id, parent_run_id=completed.parent_run_id, quality_pass=passed, reason=reason))
            if not passed:
                terminal = "quality_gate_failed"
                break
            parent = completed.run_id
            continue
        active = next((item for item in candidates if item.state in {"PLANNED", "QUEUED", "RUNNING"}), None)
        if active:
            statuses.append(ChainStageStatus(stage=stage, engine=engine, state=active.state, run_id=active.run_id, parent_run_id=active.parent_run_id, reason="registered stage is not yet completed"))
            terminal = "active"
            break
        failed = next((item for item in candidates if item.state in {"FAILED", "CANCELLED", "TIMED_OUT"}), None)
        if failed:
            statuses.append(ChainStageStatus(stage=stage, engine=engine, state=failed.state, run_id=failed.run_id, parent_run_id=failed.parent_run_id, reason="latest registered attempt failed; compile a traceable retry"))
        else:
            statuses.append(ChainStageStatus(stage=stage, engine=engine, state="NOT_SUBMITTED", parent_run_id=parent, reason="all upstream stages passed; request can be compiled"))
        next_stage = stage
        terminal = "ready"
        break
    else:
        terminal = "completed"
    return LigandFirstChainStatus(
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


def _ancestor_artifact(
    root: Path, run_id: str, engine: str, filename: str
) -> str:
    registry = ArtifactRegistry(root, create=False)
    seen: set[str] = set()
    current: str | None = run_id
    while current:
        if current in seen:
            raise ValueError("cycle detected in Ligand-first parent run chain")
        seen.add(current)
        result = registry.load_result(current)
        if result.engine == engine:
            return _artifact(root, current, filename)
        current = _scientific_parent(registry, result)
    raise ValueError(
        f"required ancestor stage {engine!r} is missing before run {run_id}"
    )


def _project_input(root: Path, value: str, label: str) -> str:
    if Path(value).suffix in {'.yaml', '.yml'}:
        path = resolve_route_config(root, value)
        if not path.is_file():
            raise ValueError(f'{label} is required before this stage: {path}')
        return config_reference(root, path)
    candidate = Path(value)
    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        relative = path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the project root: {path}") from exc
    if not path.is_file():
        raise ValueError(f"{label} is required before this stage: {relative}")
    return str(relative)


def _row_count(path: Path) -> int:
    import pandas as pd

    return len(pd.read_parquet(path, columns=["input_sdf_index"])) if path.suffix.lower() == ".parquet" else len(pd.read_csv(path, usecols=["input_sdf_index"]))


def compile_next_ligand_request(
    root: Path,
    config: LigandFirstChainConfig,
    status: LigandFirstChainStatus,
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
    if stage == "structure_intake":
        parameters = {
            "campaign": config.campaign_path,
            "restore_missing": True,
        }
        budget = ResourceBudget(
            max_runtime_minutes=15,
            cpu_count=1,
            memory_gb=4,
            max_candidates=1,
        )
    elif stage == "covalentize":
        parameters = {
            "campaign": config.campaign_path,
            "max_sites": config.max_sites,
            "max_branch_heavy_atoms": config.max_branch_heavy_atoms,
            "max_distance_angstrom": config.max_distance_angstrom,
            "max_vector_angle_degrees": config.max_vector_angle_degrees,
        }
        budget = ResourceBudget(max_runtime_minutes=30, cpu_count=2, memory_gb=8, max_candidates=config.max_sites * 12)
        if config.enumeration_backend == "upstream-native":
            parameters.update(enumeration_backend="upstream-native", upstream_repo=config.upstream_covalentizer_repo)
            budget = budget.model_copy(update={"max_candidates": config.upstream_candidate_budget})
    elif stage == "carsidock":
        assert parent
        table = _artifact(root, parent, "ligand_first_candidates.parquet")
        sdf = _artifact(root, parent, "ligand_first_candidates.sdf")
        reference_ligand = _ancestor_artifact(
            root,
            parent,
            "ligand-first-structure-intake",
            "parent_ligand_pose.sdf",
        )
        count = _row_count(root / table)
        parameters = {
            "pdb": _project_input(root, config.pdb, "CarsiDock protein structure"),
            "reference_ligand": reference_ligand,
            "ligand_sdf": sdf,
            "ligand_table": table,
            "covres": config.covres,
            "reaction_type_column": "carsidock_reaction_type",
            "device": "cuda",
            "chunk": 0,
            "num_conformer": 1,
            "cuda_device_index": config.cuda_device_index,
            "seed": config.seed,
        }
        budget = ResourceBudget(max_runtime_minutes=1440, gpu_count=1, cpu_count=4, memory_gb=64, max_candidates=count)
    elif stage == "carsidock_selection":
        assert parent
        table = _artifact(root, parent, "carsidock_scores.parquet")
        import pandas as pd

        score_frame = pd.read_parquet(root / table, columns=["engine_status", "raw_score"])
        success_count = int(
            (score_frame["engine_status"].eq("SUCCESS") & score_frame["raw_score"].notna()).sum()
        )
        if success_count < 1:
            raise ValueError("CarsiDock produced no successful candidate for AF3 handoff")
        top_n = min(config.carsidock_top_n, success_count)
        union = round(top_n * 0.80)
        parameters = {
            "carsidock_scores": table,
            "top_n": top_n,
            "score_union_n": union,
            "diversity_n": top_n - union,
            "rescue_n": 0,
            "min_carsidock_success_rate": config.min_carsidock_success_rate,
        }
        budget = ResourceBudget(max_runtime_minutes=30, cpu_count=4, memory_gb=16, max_candidates=top_n)
    elif stage == "af3":
        assert parent
        table = _artifact(root, parent, "stage3_recall_pool.parquet")
        count = _row_count(root / table)
        parameters = {
            "candidate_table": table,
            "source_data_json": _project_input(
                root, config.af3_source_data_json, "AF3 protein seed"
            ),
            "target_config": _project_input(
                root, config.target_config, "AF3 target config"
            ),
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
        budget = ResourceBudget(max_runtime_minutes=10080, gpu_count=1, cpu_count=4, memory_gb=96, max_candidates=count)
    elif stage == "covdock_lo":
        assert parent
        table = _artifact(root, parent, "stage4_top_candidates.parquet")
        count = _row_count(root / table)
        slots = min(config.covdock_cpu_count, count)
        parameters = {
            "candidate_table": table,
            "receptor": _project_input(
                root, config.covdock_receptor, "CovDock receptor"
            ),
            "attachment_residue": config.attachment_residue,
            "nucleophilic_atom": "SG",
            "reaction_type_column": "covdock_reaction_type",
            "checkpoint_size": min(config.covdock_checkpoint_size, count),
            "final_n": min(config.covdock_top_n, count),
            "njobs": slots,
            "min_success_rate": config.min_covdock_success_rate,
            "resume": True,
        }
        budget = ResourceBudget(max_runtime_minutes=10080, cpu_count=config.covdock_cpu_count, memory_gb=128, max_candidates=count)
    elif stage == "independent_evaluation":
        assert parent
        table = _artifact(root, parent, "stage5_gated_candidates.parquet")
        count = _row_count(root / table)
        parameters = {
            "candidate_table": table,
            "campaign": config.campaign_path,
            "min_parent_mcs_fraction": config.min_parent_mcs_fraction,
            "recheck_coordinates": True,
            "attachment_residue": config.attachment_residue,
        }
        # Keep the compiled job inside the native three-candidate execution
        # boundary.  The campaign-wide approval may cover a longer sequence,
        # but the external Inspire dispatcher admits at most 15 minutes for
        # this individual CPU evaluation stage.
        budget = ResourceBudget(max_runtime_minutes=15, cpu_count=4, memory_gb=16, max_candidates=count)
    else:
        assert parent
        table = _artifact(root, parent, "ligand_first_evaluated_candidates.parquet")
        count = _row_count(root / table)
        parameters = {
            "candidate_table": table,
            "final_n": min(config.final_n, count),
            "_pareto_profile": "ligand-first",
            "trace_parent_run_id": parent,
            "trace_chain_config": config.trace_chain_config,
        }
        # Pareto is the second bounded CPU post-processing stage and has the
        # same 15-minute per-stage ceiling as independent evaluation.
        budget = ResourceBudget(max_runtime_minutes=15, cpu_count=2, memory_gb=16, max_candidates=count)
    scientific_parent = parent
    job_parent = parent
    if scientific_parent:
        parameters["upstream_run_id"] = scientific_parent
    if config.dispatch_policy is not None:
        parameters.update(
            dispatch_policy=config.dispatch_policy,
            dispatch_batch=config.dispatch_batch,
        )
        # parent_run_id is reserved for retry ancestry by the external
        # dispatcher.  Scientific stage lineage is immutable in
        # parameters.upstream_run_id and is used by chain inspection and trace
        # generation, so an ordinary downstream stage remains attempt 1.
        job_parent = None
    return JobSubmissionRequest(
        request_id=f"{config.campaign_id}-{stage}-{execution_mode}-v1",
        project_id=config.project_id,
        route="ligand-first",
        engine=STAGE_ENGINES[stage],  # type: ignore[arg-type]
        parameters=parameters,
        resource_budget=budget,
        execution_mode=execution_mode,
        human_approved=human_approved,
        approval_note=approval_note,
        parent_run_id=job_parent,
    )


def ligand_chain_provenance(root: Path, config_path: Path) -> dict[str, Any]:
    config = load_ligand_chain_config(config_path.resolve())
    return {
        "config_path": config_reference(root, config_path),
        "config_sha256": sha256_file(config_path.resolve()),
        "stage_order": list(config.stage_order),
        "engines": STAGE_ENGINES,
        "interpretation": "computational prioritization; experimental validation remains required",
    }
