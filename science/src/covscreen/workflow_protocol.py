"""Shared workflow mechanics; scientific stages remain owned by each route.

All mutations run under the caller's existing workflow lock. No function here
submits a job, grants approval or infers scientific success from task completion.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .workflow_store import save


def revision(state: dict, expected: int):
    if type(expected) is not int or expected != state["revision"]:
        raise ValueError(f"STALE_WORKFLOW_REVISION: read status; current revision={state['revision']}")


def changed(folder: Path, state: dict, operation: str, details: dict | None = None):
    if details and {"revision", "operation"} & details.keys():
        raise ValueError("Event details cannot replace workflow revision or operation")
    state["revision"] += 1
    state["events"].append({"revision": state["revision"], "operation": operation, **(details or {})})
    save(folder, state)


def archive_branch(state: dict, reason: str, *, retain: tuple[str, ...], invalidate: tuple[str, ...]):
    """Route declares affected assets; unrelated assets are left intact."""
    if any(state.get(key) is not None for key in invalidate):
        state["branches"].append({"revision": state["revision"], "reason": reason,
            "stage": state["stage"], **{key: deepcopy(state.get(key)) for key in retain}})
    for key in invalidate:
        state[key] = None


def begin_operation(folder: Path, state: dict, tool: str, action: str, *, submission: dict | None = None,
                    recovery: dict | None = None):
    if state.get("pending"):
        raise ValueError("Uncertain operation requires reconciliation; do not replay")
    state["pending"] = {"tool": tool, "action": action, "operation_id": uuid4().hex}
    if submission is not None:
        state["pending"]["submission"] = deepcopy(submission)
    if recovery is not None:
        state["pending"]["recovery"] = deepcopy(recovery)
    save(folder, state)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ToolView(StrictModel):
    actions: list[str] = Field(min_length=1)
    fields: list[str]
    field_constraints: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def constraint_fields(self):
        if (set(self.field_constraints) - set(self.fields)
                or set(self.field_constraints) & {"action", "workflow_id", "expected_revision", "request_id"}):
            raise ValueError("Stage constraints must only narrow exposed scientific fields")
        return self


class WorkflowView(StrictModel):
    protocol: str
    workflow_id: str
    revision: int = Field(ge=1)
    stage: str
    tools: dict[str, ToolView]


class Execution(StrictModel):
    # The scope is explicit: local evidence/graph generation can run while no
    # registered docking/MD/generation-engine compute job has been submitted.
    scope: str
    state: Literal["not_submitted", "submitted", "running", "completed", "failed", "outcome_unknown"]
    task_ids: list[str] = Field(default_factory=list)


class Assessment(StrictModel):
    scope: str
    state: Literal["not_evaluated", "evidence_insufficient", "passed", "rejected", "review_required"]
    reason: str


class Artifact(StrictModel):
    kind: str
    id: str
    sha256: str | None = Field(default=None, pattern="^[a-f0-9]{64}$")
    hash_scope: Literal["manifest", "artifact"] | None = None

    @model_validator(mode="after")
    def digest_scope(self):
        if (self.sha256 is None) != (self.hash_scope is None):
            raise ValueError("An artifact digest requires its exact hash scope")
        return self


class Blocker(StrictModel):
    code: str
    scope: str
    reason: str
    recoverable: bool


class WorkflowEnvelope(BaseModel):
    # Existing route result fields remain available during protocol migration.
    model_config = ConfigDict(extra="allow", strict=True)
    schema_version: Literal["research-workflow-result-v1"] = "research-workflow-result-v1"
    route: Literal["ligand-first", "library-first", "de-novo"]
    workflow: WorkflowView
    execution: Execution
    assessment: Assessment
    artifacts: list[Artifact] = Field(default_factory=list)
    blockers: list[Blocker] = Field(default_factory=list)


def envelope(**value: Any) -> dict:
    result = WorkflowEnvelope.model_validate(value).model_dump(mode="json")
    # Keep earlier workflow views byte-shape compatible when no narrowing is used.
    for tool in result["workflow"]["tools"].values():
        if not tool["field_constraints"]:
            tool.pop("field_constraints")
    if result["route"] in {"library-first", "ligand-first"} and result["workflow"]["stage"] == "TARGET_REVIEW":
        selection = any("select_site" in tool["actions"] for tool in result["workflow"]["tools"].values())
        result["stage_guidance"] = {
            "schema": "target-review-guidance-v1", "role": "interpretation_only_not_permission",
            "select_site": {
                "workflow_action_available": selection,
                "purpose": "Record a reasoned exploratory hypothesis on a verified canonical cysteine",
                "requires_structural_suitability_evidence_before_hypothesis": False,
                "establishes_reactivity": False,
            },
            "site_assessment_required_evidence_applies_to": "Subsequent site suitability and reaction-context assessment, not permission to record an exploratory site hypothesis",
            "next_step": ("If within the user's requested scope, select a verified canonical cysteine with a reason; then gather structural mapping, accessibility and reaction-context evidence. Do not describe selection as validated chemistry."
                if selection else "Resolve the target or reconcile the current blocker according to the exposed workflow actions before selecting a site."),
            "user_scope": "Do not select a site when the user requested identity review only or asked to choose the site themselves.",
        }
    return result


def output_schema(route: str, protocol: str, pattern: str, names: tuple[str, ...]) -> dict:
    schema = WorkflowEnvelope.model_json_schema()
    schema["properties"]["route"] = {"type": "string", "const": route}
    view = schema["$defs"]["WorkflowView"]
    view["properties"]["protocol"] = {"type": "string", "const": protocol}
    view["properties"]["workflow_id"] = {"type": "string", "pattern": pattern}
    view["properties"]["tools"]["propertyNames"] = {"enum": list(names)}
    definitions = schema.pop("$defs")
    # Existing transports and the installed SDK also validate structured tool
    # errors. Preserve their typed FAIL envelope without fabricating a workflow.
    return {"type": "object", "$defs": definitions, "anyOf": [schema,
        {"type": "object", "properties": {"status": {"const": "FAIL"}},
         "required": ["status"], "additionalProperties": True}]}
