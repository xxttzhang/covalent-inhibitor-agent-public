"""Single source for site-discovery schemas and strict, side-effect-free validation."""
from __future__ import annotations

from copy import deepcopy
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
RunId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,199}$")]


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SiteBudget(StrictInput):
    max_runtime_minutes: int = Field(default=20, ge=1, le=60)
    gpu_count: int = Field(default=0, ge=0, le=0)
    cpu_count: int = Field(default=2, ge=1, le=4)
    memory_gb: float = Field(default=8, gt=0, le=16)
    max_candidates: int = Field(default=100, ge=1, le=1000)


class PredictionPlan(StrictInput):
    action: Literal["plan"] = "plan"
    receptor_input_artifact_id: str = Field(pattern=r"^receptor-input-[a-f0-9]{32}$",
        description="Copy artifact_id from covalent_acquire_structure; never invent an ID.")
    manifest_sha256: Sha = Field(description="Required: copy manifest_sha256 from the SAME acquisition result as receptor_input_artifact_id.")
    native_timeout_seconds: int = Field(default=900, ge=60, le=3600)
    resource_budget: SiteBudget = Field(default_factory=SiteBudget,
        description="Omit to use defaults: 2 CPUs, no GPU, 8 GB, 20 minutes, 100 candidates. Do not choose ceilings as defaults. Any change is bound into native approval.")

    @model_validator(mode="after")
    def timeout_fits_budget(self):
        if self.native_timeout_seconds > self.resource_budget.max_runtime_minutes * 60:
            raise ValueError("native_timeout_seconds exceeds resource_budget.max_runtime_minutes")
        return self


class PredictionStart(PredictionPlan):
    action: Literal["start"]
    human_approved: bool = Field(default=False,
        description="Compatibility field only; true does not grant approval. The host must obtain native approval for the exact execution scope.")
    approval_note: str | None = None


class PredictionStatus(StrictInput):
    action: Literal["status"]
    run_id: RunId = Field(description="Use the run_id returned by an actually submitted prediction; a planned run is not a submitted job.")


class EvaluationInput(StrictInput):
    prediction_run_id: RunId
    prediction_manifest_sha256: Sha = Field(description="Copy prediction_manifest_sha256 from the completed prediction status response.")
    hypothesis_evidence_ids: list[Annotated[str, Field(pattern=r"^hyp-[a-f0-9]{24}$")]] = Field(default_factory=list, description="Optional sealed evidence IDs. Omit or use [] when literature is unavailable; missing evidence remains unknown and does not block DeepCoSI-based exploratory selection.", json_schema_extra={"uniqueItems": True})

    @model_validator(mode="after")
    def unique_evidence(self):
        if len(set(self.hypothesis_evidence_ids)) != len(self.hypothesis_evidence_ids):
            raise ValueError("hypothesis_evidence_ids must be unique")
        return self


class SelectionInput(StrictInput):
    evaluation_artifact_id: str = Field(pattern=r"^site-evaluation-[a-f0-9]{32}$")
    evaluation_manifest_sha256: Sha = Field(description="Copy manifest_sha256 from the SAME evaluation result as evaluation_artifact_id.")
    canonical_positions: list[Annotated[int, Field(ge=1)]] | None = Field(default=None,
        json_schema_extra={"uniqueItems": True}, min_length=1, max_length=10)
    max_sites: int = Field(default=1, ge=1, le=10)
    routes: list[Literal["library-first", "ligand-first", "de-novo"]] = Field(
        default_factory=lambda: ["library-first", "ligand-first", "de-novo"],
        min_length=1, max_length=3, json_schema_extra={"uniqueItems": True})

    @model_validator(mode="after")
    def selection_bounds(self):
        positions = self.canonical_positions
        if positions is not None and (len(set(positions)) != len(positions) or len(positions) > self.max_sites):
            raise ValueError("canonical_positions must be unique and within max_sites")
        if len(set(self.routes)) != len(self.routes):
            raise ValueError("routes must be unique")
        return self


def validate_prediction(arguments: dict) -> dict:
    action = arguments.get("action", "plan")
    model = {"plan": PredictionPlan, "start": PredictionStart, "status": PredictionStatus}.get(action) if isinstance(action, str) else None
    if model is None:
        raise ValueError("action must be plan, start, or status")
    return model.model_validate(arguments).model_dump()


def input_schema(model: type[BaseModel]) -> dict:
    """Inline local references for MCP clients that do not resolve $defs."""
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if isinstance(value, dict):
            if "$ref" in value:
                value = {**deepcopy(definitions[value["$ref"].split("/")[-1]]),
                         **{key: item for key, item in value.items() if key != "$ref"}}
            return {key: expand(item) for key, item in value.items() if key != "title"}
        return value

    return expand(schema)


def prediction_schema() -> dict:
    branches = [input_schema(model) for model in (PredictionPlan, PredictionStart, PredictionStatus)]
    properties = {key: value for branch in branches for key, value in branch["properties"].items()}
    properties["action"] = {"type": "string", "enum": ["plan", "start", "status"], "default": "plan"}
    return {"type": "object", "properties": properties, "oneOf": branches,
            "additionalProperties": False}
