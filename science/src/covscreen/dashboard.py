from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jobs import ArtifactRegistry, RunResult, utc_now


STATE_MAP = {
    "PLANNED": "queued",
    "QUEUED": "queued",
    "RUNNING": "running",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "CANCELLED": "failed",
    "TIMED_OUT": "failed",
}


@dataclass(frozen=True)
class StageDefinition:
    id: str
    label: str
    engine: str
    manifest_names: tuple[str, ...]
    count_keys: tuple[str, ...]
    input_count_keys: tuple[str, ...] = ()


COMPUTE_STAGES = (
    StageDefinition(
        "carsidock",
        "CarsiDock-Cov",
        "carsidock-cov",
        ("carsidock_scores_audit.json",),
        ("success_rows",),
        ("input_rows",),
    ),
    StageDefinition(
        "rtmscore",
        "RTMScore",
        "rtmscore",
        ("rtmscore.run_manifest.json",),
        ("scored_row_count",),
        ("output_row_count", "input_pose_count"),
    ),
    StageDefinition(
        "recall_pool",
        "高召回候选池",
        "carsidock-pool-selection",
        ("stage3_selection_manifest.json",),
        ("selected_identity_count",),
        ("input_identity_count",),
    ),
    StageDefinition(
        "af3",
        "AF3–mPAE",
        "alphafold3-mpae",
        ("stage4_selection_manifest.json",),
        ("selected_count", "success_count"),
        ("input_identity_count",),
    ),
    StageDefinition(
        "covdock",
        "CovDock-LO 门控",
        "schrodinger-covdock-lo",
        ("stage5_gate_manifest.json", "covdock_lo_registered_manifest.json"),
        ("final_candidate_count", "covdock_success_count"),
        ("input_identity_count", "input_count"),
    ),
    StageDefinition(
        "pareto",
        "Pareto 实验候选",
        "library-first-pareto",
        ("stage6_pareto_manifest.json",),
        ("selected_count",),
        ("input_count", "eligible_count"),
    ),
)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _first_number(payloads: list[dict[str, Any]], keys: tuple[str, ...]) -> int | None:
    containers: list[dict[str, Any]] = []
    for payload in payloads:
        containers.extend(
            value
            for value in (
                payload,
                payload.get("counts"),
                payload.get("quality"),
                payload.get("gate_quality"),
            )
            if isinstance(value, dict)
        )
    for key in keys:
        for container in containers:
            value = container.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
    return None


def _quality(payloads: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, bool]]:
    quality: dict[str, Any] = {}
    gates: dict[str, bool] = {}
    for payload in payloads:
        for key in ("quality", "gate_quality"):
            value = payload.get(key)
            if isinstance(value, dict):
                quality.update(value)
        for key in ("quality_gates", "gate_quality_gates"):
            value = payload.get(key)
            if isinstance(value, dict):
                gates.update(
                    (str(name), bool(passed))
                    for name, passed in value.items()
                    if isinstance(passed, bool)
                )
    return quality, gates


def _latest_jobs(
    registry: ArtifactRegistry, preferred_run_id: str | None
) -> tuple[str | None, dict[str, RunResult]]:
    results = registry.list_results(limit=1000)
    preferred: RunResult | None = None
    if preferred_run_id:
        try:
            preferred = registry.load_result(preferred_run_id)
        except (OSError, ValueError):
            preferred = None
    project_id = preferred.project_id if preferred and preferred.route == "library-first" else None
    if project_id is None:
        project_id = next(
            (result.project_id for result in results if result.route == "library-first"),
            None,
        )
    latest: dict[str, RunResult] = {}
    if project_id is not None:
        for result in results:
            if result.project_id == project_id and result.route == "library-first":
                latest.setdefault(result.engine, result)
    if preferred is not None and preferred.project_id == project_id:
        latest[preferred.engine] = preferred
    return project_id, latest


def _formal_chain_jobs(
    root: Path, registry: ArtifactRegistry
) -> tuple[str | None, dict[str, RunResult], dict[str, Any] | None]:
    """Prefer the newest declared parent-linked campaign over orphan/legacy jobs."""

    chain_files = sorted(
        (root / "state" / "chains").glob("*/chain_status.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for chain_file in chain_files:
        payload = _read_json(chain_file)
        provenance = payload.get("provenance")
        if not isinstance(provenance, dict):
            continue
        config_value = provenance.get("config_path")
        if not isinstance(config_value, str):
            continue
        config_path = (root / config_value).resolve()
        try:
            config_path.relative_to(root)
            from .library_chain import inspect_chain, load_chain_config

            config = load_chain_config(config_path)
            status = inspect_chain(root, config)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        jobs: dict[str, RunResult] = {}
        for stage in status.stages:
            if stage.run_id is None:
                continue
            try:
                result = registry.load_result(stage.run_id)
            except (OSError, ValueError):
                continue
            jobs[result.engine] = result
        return config.project_id, jobs, status.model_dump(mode="json")
    return None, {}, None


def _job_payloads(
    registry: ArtifactRegistry,
    job: RunResult,
    names: tuple[str, ...],
) -> list[dict[str, Any]]:
    artifacts = registry.job_dir(job.run_id) / "artifacts"
    payloads = [_read_json(artifacts / name) for name in names]
    return [payload for payload in payloads if payload]


def _stage(
    registry: ArtifactRegistry,
    definition: StageDefinition,
    job: RunResult | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bool]]:
    if job is None:
        return (
            {"id": definition.id, "label": definition.label, "status": "pending"},
            {},
            {},
        )
    payloads = _job_payloads(registry, job, definition.manifest_names)
    quality, gates = _quality(payloads)
    count = _first_number(payloads, definition.count_keys)
    input_count = _first_number(payloads, definition.input_count_keys)
    stage: dict[str, Any] = {
        "id": definition.id,
        "label": definition.label,
        "status": STATE_MAP[job.state],
        "run_id": job.run_id,
        "updated_at": job.finished_at or job.started_at or job.submitted_at,
    }
    if count is not None:
        stage["count"] = count
    if input_count is not None:
        stage["input_count"] = input_count
    if input_count and count is not None:
        stage["success_rate"] = count / input_count
    if gates:
        stage["quality_gates"] = gates
    return stage, quality, gates


def _prescreen_stages(
    registry: ArtifactRegistry, job: RunResult | None
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, bool], dict[str, Any]]:
    if job is None:
        pending = [
            {"id": "catalog", "label": "CovalentInDB", "status": "pending"},
            {"id": "qc", "label": "化学质控", "status": "pending"},
            {"id": "prescreen", "label": "二维预筛", "status": "pending"},
        ]
        return pending, {}, {}, {}
    artifact_dir = registry.job_dir(job.run_id) / "artifacts"
    progress = _read_json(artifact_dir / "progress.json")
    manifest = _read_json(artifact_dir / "stage2_selection_manifest.json")
    payloads = [payload for payload in (progress, manifest) if payload]
    counts = next(
        (payload["counts"] for payload in payloads if isinstance(payload.get("counts"), dict)),
        {},
    )
    quality, gates = _quality(payloads)
    spec = registry.load_spec(job.run_id)
    maximum = spec.parameters.get("max_rows")
    processed = int(counts.get("input_rows") or 0)
    selected = int(counts.get("selected") or 0)
    unique = int(counts.get("unique") or 0)
    state = STATE_MAP[job.state]
    observed = min(100.0, processed / int(maximum) * 100.0) if maximum else 0.0
    if job.state == "COMPLETED":
        observed = 100.0
    common = {
        "status": state,
        "run_id": job.run_id,
        "updated_at": job.finished_at or job.started_at or job.submitted_at,
    }
    stages = [
        {"id": "catalog", "label": "CovalentInDB", "count": processed, **common},
        {"id": "qc", "label": "化学质控", "count": unique, **common},
        {
            "id": "prescreen",
            "label": "二维预筛",
            "count": selected,
            "input_count": processed,
            "quality_gates": gates,
            **common,
        },
    ]
    context = {
        "manifest": manifest,
        "spec": spec,
        "processed": processed,
        "selected": selected,
        "observed": observed,
    }
    return stages, quality, gates, context


def _overall_state(stages: list[dict[str, Any]]) -> str:
    if any(stage["status"] == "running" for stage in stages):
        return "running"
    if any(stage["status"] == "failed" for stage in stages):
        return "failed"
    if stages and all(stage["status"] == "completed" for stage in stages):
        return "completed"
    return "queued"


def _outputs(
    root: Path,
    registry: ArtifactRegistry,
    jobs: dict[str, RunResult],
    existing: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    generated: list[dict[str, Any]] = []
    preferred = {
        "stage2_selection_manifest.json": "CovalentInDB 预筛清单",
        "stage2_candidates.parquet": "二维预筛候选",
        "carsidock_scores.parquet": "CarsiDock-Cov 分数",
        "rtmscore.parquet": "RTMScore 分数",
        "stage3_recall_pool.parquet": "高召回候选池",
        "stage4_top_candidates.parquet": "AF3 Top 候选",
        "stage5_gated_candidates.parquet": "CovDock-LO 门控结果",
        "stage6_final_candidates.parquet": "最终 Pareto 候选",
        "stage6_candidate_cards.json": "实验候选卡片",
    }
    for job in jobs.values():
        job_dir = registry.job_dir(job.run_id)
        artifact_dir = job_dir / "artifacts"
        for name, label in preferred.items():
            path = artifact_dir / name
            if path.is_file():
                relative = str(path.relative_to(root))
                generated.append(
                    {
                        "label": label,
                        "path": relative,
                        "kind": "manifest" if path.suffix == ".json" else "dataset",
                        "note": job.run_id,
                    }
                )
    seen: set[str] = set()
    deduplicated: list[dict[str, Any]] = []
    for item in [*generated, *existing]:
        path = str(item.get("path", ""))
        if not path or path in seen:
            continue
        seen.add(path)
        deduplicated.append(item)
    return deduplicated


def refresh_dashboard(
    project_root: str | Path, preferred_run_id: str | None = None
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    output = root / "results" / "dashboard.json"
    dashboard = _read_json(output)
    dashboard["schema_version"] = "1.2"
    dashboard.setdefault("campaign_name", "共价抑制剂三路线研发平台")
    dashboard.setdefault("tasks", [])
    dashboard.setdefault("candidates", [])
    dashboard.setdefault("outputs", [])
    registry = ArtifactRegistry(root, create=False)
    project_id, jobs, chain_status = _formal_chain_jobs(root, registry)
    if project_id is None:
        project_id, jobs = _latest_jobs(registry, preferred_run_id)
    if project_id is None:
        dashboard["generated_at"] = utc_now()
        _write_json(output, dashboard)
        return dashboard

    prescreen = jobs.get("covindb-prescreen")
    early_stages, early_quality, early_gates, context = _prescreen_stages(
        registry, prescreen
    )
    stages = list(early_stages)
    quality_by_stage: dict[str, Any] = {"prescreen": early_quality}
    gates: dict[str, bool] = {
        f"prescreen:{key}": value for key, value in early_gates.items()
    }
    for definition in COMPUTE_STAGES:
        stage, quality, stage_gates = _stage(
            registry, definition, jobs.get(definition.engine)
        )
        stages.append(stage)
        if quality:
            quality_by_stage[definition.id] = quality
        gates.update(
            (f"{definition.id}:{key}", value)
            for key, value in stage_gates.items()
        )

    latest_job = next(
        (jobs[definition.engine] for definition in reversed(COMPUTE_STAGES) if definition.engine in jobs),
        prescreen,
    )
    selected = next(
        (int(stage["count"]) for stage in reversed(stages) if "count" in stage),
        0,
    )
    completed_units = sum(stage["status"] == "completed" for stage in stages)
    running_units = sum(stage["status"] == "running" for stage in stages)
    progress = round((completed_units + 0.5 * running_units) / len(stages) * 100.0, 1)
    manifest = context.get("manifest", {})
    spec = context.get("spec")
    target = (
        manifest.get("target", {}).get("site_id")
        if isinstance(manifest.get("target"), dict)
        else None
    ) or project_id
    source = (
        spec.provenance.get("dataset", "CovalentInDB 2.0")
        if spec is not None
        else "CovalentInDB 2.0"
    )
    state = _overall_state(stages)
    dashboard["library_first"] = {
        "run_id": latest_job.run_id if latest_job else "not-started",
        "state": state,
        "target": target,
        "source": source,
        "processed": int(context.get("processed", 0)),
        "selected": selected,
        "progress": progress,
        "quality": quality_by_stage,
        "quality_gates": gates,
        "stages": stages,
    }
    if chain_status is not None:
        dashboard["library_first"].update(
            {
                "campaign_id": chain_status.get("campaign_id"),
                "next_stage": chain_status.get("next_stage"),
                "chain_terminal_state": chain_status.get("terminal_state"),
            }
        )

    stage_tasks = []
    for stage in stages:
        if stage["status"] == "pending":
            continue
        stage_tasks.append(
            {
                "id": f"library-first-{stage['id']}",
                "name": stage["label"],
                "route": "library-first",
                "status": stage["status"],
                "progress": 100.0 if stage["status"] == "completed" else 50.0 if stage["status"] == "running" else 0.0,
                "detail": (
                    f"输入 {stage.get('input_count', '—')}，当前输出 {stage.get('count', '—')}。"
                ),
                "updated_at": stage.get("updated_at"),
                "run_id": stage.get("run_id"),
            }
        )
    old_tasks = [
        item
        for item in dashboard["tasks"]
        if not str(item.get("id", "")).startswith("library-first-")
    ]
    dashboard["tasks"] = [*reversed(stage_tasks), *old_tasks]
    dashboard["outputs"] = _outputs(
        root, registry, jobs, list(dashboard["outputs"])
    )
    dashboard["generated_at"] = utc_now()
    _write_json(output, dashboard)
    return dashboard


def try_refresh_dashboard(
    project_root: str | Path, preferred_run_id: str | None = None
) -> bool:
    try:
        refresh_dashboard(project_root, preferred_run_id)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return True
