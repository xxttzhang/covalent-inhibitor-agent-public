from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


JobRoute = Literal["shared", "library-first", "ligand-first", "de-novo"]
JobEngine = Literal[
    "shared-deepcosi-site-prediction",
    "library-first-receptor-preparation",
    "library-first-adduct-parameterization",
    "library-first-openmm-system-preparation",
    "library-first-openmm-short-qc",
    "library-first-openmm-equilibration",
    "library-first-openmm-production",
    "library-first-trajectory-audit",
    "library-first-structural-analysis",
    "library-first-md-report",
    "library-first-md-candidate-delivery",
    "library-first-md-adduct-admission",
    "covindb-prescreen",
    "rdkit-warhead-enumeration",
    "ligand-first-structure-intake",
    "ligand-first-covalentization",
    "carsidock-cov",
    "rtmscore",
    "carsidock-pool-selection",
    "alphafold3-mpae",
    "schrodinger-covdock-lo",
    "schrodinger-covdock-vs",
    "de-novo-prereaction-vina",
    "de-novo-prereaction-glide",
    "ligand-first-evaluation",
    "ligand-first-pareto",
    "library-first-pareto",
    "library-first-evaluation",
    "pocketxmol",
    "reinvent4",
    "difflinker",
]
JobState = Literal[
    "PLANNED",
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
]
ArtifactRole = Literal[
    "input",
    "candidate_table",
    "pose",
    "score_table",
    "manifest",
    "log",
    "report",
    "other",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ResourceBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_runtime_minutes: int = Field(ge=1, le=10080)
    gpu_count: int = Field(default=0, ge=0, le=8)
    cpu_count: int = Field(default=1, ge=1, le=128)
    memory_gb: float = Field(default=4.0, gt=0, le=1024)
    max_candidates: int = Field(default=1, ge=1, le=1000000)


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    run_id: str
    role: ArtifactRole
    path: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str
    created_at: str = Field(default_factory=utc_now)


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    run_id: str
    request_id: str
    project_id: str
    route: JobRoute
    engine: JobEngine
    command: list[str] = Field(min_length=1)
    working_directory: str
    output_directory: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    input_paths: list[str] = Field(default_factory=list)
    input_sha256: dict[str, str] = Field(default_factory=dict)
    expected_output_paths: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    resource_budget: ResourceBudget
    execution_mode: Literal["plan", "execute"] = "plan"
    human_approved: bool = False
    approval_note: str | None = None
    parent_run_id: str | None = None
    attempt: int = Field(default=1, ge=1, le=100)
    created_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def approval_gate(self) -> "JobSpec":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{5,127}", self.run_id):
            raise ValueError("run_id must be a safe 6-128 character identifier")
        if self.execution_mode == "execute":
            if not self.human_approved:
                raise ValueError("execute mode requires human_approved=true")
            if not self.approval_note or len(self.approval_note.strip()) < 8:
                raise ValueError("execute mode requires a meaningful approval_note")
        return self


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    run_id: str
    state: JobState
    engine: JobEngine
    route: JobRoute
    project_id: str
    attempt: int = Field(default=1, ge=1, le=100)
    parent_run_id: str | None = None
    submitted_at: str
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    engine_pid: int | None = None
    return_code: int | None = None
    runtime_seconds: float | None = Field(default=None, ge=0)
    message: str
    stdout_path: str
    stderr_path: str
    candidate_table_path: str | None = None
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    failure_reason: str | None = None


class JobSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    project_id: str
    route: JobRoute
    engine: JobEngine
    parameters: dict[str, Any] = Field(default_factory=dict)
    resource_budget: ResourceBudget
    execution_mode: Literal["plan", "execute"] = "plan"
    human_approved: bool = False
    approval_note: str | None = None
    parent_run_id: str | None = None
    attempt: int = Field(default=1, ge=1, le=100)


class ArtifactRegistry:
    """Filesystem registry with atomic JSON writes and restart-safe state."""

    def __init__(self, project_root: str | Path, *, create: bool = True):
        self.project_root = Path(project_root).resolve()
        self.root = self.project_root / "state" / "jobs"
        if create:
            self.root.mkdir(parents=True, exist_ok=True)

    def job_dir(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{5,127}", run_id):
            raise ValueError("unsafe run_id")
        path = (self.root / run_id).resolve()
        path.relative_to(self.root)
        return path

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(path)

    def create(self, spec: JobSpec, result: RunResult) -> Path:
        job_dir = self.job_dir(spec.run_id)
        job_dir.mkdir(parents=True, exist_ok=False)
        self._write_json(job_dir / "spec.json", spec.model_dump(mode="json"))
        self._write_json(job_dir / "result.json", result.model_dump(mode="json"))
        return job_dir

    def write_result(self, result: RunResult) -> None:
        self._write_json(
            self.job_dir(result.run_id) / "result.json",
            result.model_dump(mode="json"),
        )

    def write_spec(self, spec: JobSpec) -> None:
        self._write_json(
            self.job_dir(spec.run_id) / "spec.json",
            spec.model_dump(mode="json"),
        )

    def load_spec(self, run_id: str) -> JobSpec:
        return JobSpec.model_validate_json((self.job_dir(run_id) / "spec.json").read_text())

    def load_result(self, run_id: str) -> RunResult:
        return RunResult.model_validate_json((self.job_dir(run_id) / "result.json").read_text())

    def list_results(self, *, limit: int = 100) -> list[RunResult]:
        if not self.root.is_dir():
            return []
        results: list[RunResult] = []
        for path in sorted(self.root.glob("*/result.json"), reverse=True):
            try:
                results.append(RunResult.model_validate_json(path.read_text()))
            except (OSError, ValueError):
                continue
            if len(results) >= limit:
                break
        return results

    def register_file(self, run_id: str, path: Path, role: ArtifactRole) -> ArtifactRecord:
        path = path.resolve()
        path.relative_to(self.project_root)
        stat = path.stat()
        digest = sha256_file(path)
        suffix = path.suffix.lower()
        media_type = {
            ".json": "application/json",
            ".csv": "text/csv",
            ".parquet": "application/vnd.apache.parquet",
            ".sdf": "chemical/x-mdl-sdfile",
            ".pdb": "chemical/x-pdb",
            ".log": "text/plain",
            ".md": "text/markdown",
        }.get(suffix, "application/octet-stream")
        return ArtifactRecord(
            artifact_id=f"sha256:{digest}",
            run_id=run_id,
            role=role,
            path=str(path.relative_to(self.project_root)),
            media_type=media_type,
            size_bytes=stat.st_size,
            sha256=digest,
        )

    def tail_log(self, run_id: str, stream: Literal["stdout", "stderr"], max_lines: int = 200) -> str:
        result = self.load_result(run_id)
        path = Path(result.stdout_path if stream == "stdout" else result.stderr_path)
        if not path.is_absolute():
            path = self.project_root / path
        path = path.resolve()
        path.relative_to(self.job_dir(run_id))
        if not path.is_file():
            return ""
        return "\n".join(path.read_text(errors="replace").splitlines()[-max_lines:])
