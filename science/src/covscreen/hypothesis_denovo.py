"""Plan-only bridge from evidence-backed hypotheses to the real De novo planner."""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .de_novo import DeNovoRequest


def handoff(root: Path, evidence: dict, warheads: list[str], profile: dict | None) -> dict:
    blocked = {"state": "UNSUPPORTED", "intake": {"state": "TARGET_NOT_REGISTERED", "intent": "de-novo"},
        "next_call": None, "compute_submitted": False,
        "compatibility_blockers": ["Register a target/site/species-specific De novo request with verified pocket inputs"]}
    if not profile or not profile.get("request_path"):
        return blocked
    root = root.resolve()
    source = (root / profile["request_path"]).resolve()
    source.relative_to(root)
    raw = source.read_bytes()
    request = DeNovoRequest.model_validate_json(raw)
    from .tool_api import load_project_state, plan_de_novo_tool
    project, passport, _, _ = load_project_state(root, request.project_id)
    source.relative_to(project)
    expected = (evidence["target"], evidence["identity"].get("uniprot_id"), evidence["site"])
    actual = (passport.symbol, passport.uniprot_id, "C" + str(passport.site.residue_number))
    if expected != actual or passport.site.residue_name.upper() != "CYS":
        raise ValueError("De novo passport does not match hypothesis target/site")
    if evidence["taxon"] != 9606 or passport.organism != "Homo sapiens":
        return {**blocked, "compatibility_blockers": ["Species mapping is not validated by this De novo adapter"]}
    if not evidence["identity"].get("canonical_cys_verified"):
        return {**blocked, "compatibility_blockers": ["Canonical cysteine verification required"]}
    if set(warheads) != set(request.allowed_warheads):
        return {**blocked, "compatibility_blockers": ["Hypothesis warheads differ from registered generation request"]}
    historical = [name for name in ("scaffold_candidates_path", "covalent_candidates_path",
        "optimized_candidates_path", "covalent_screen_results_path") if getattr(request, name)]
    if historical:
        raise ValueError("New De novo plan cannot inherit historical candidate artifacts: " + ", ".join(historical))
    # Unique request, no approval inheritance, and no rewriting of the source template.
    values = request.model_dump(mode="json")
    values.update(request_id="hypothesis-denovo-" + uuid4().hex,
        route_override_approved=False, human_approved_generation=False, human_approved_covalent_screen=False)
    fresh = DeNovoRequest.model_validate(values)
    destination = project / "plans" / (fresh.request_id + ".json")
    destination.resolve().relative_to(root)
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(fresh.model_dump(mode="json"), stream, indent=2)
    relative = str(destination.relative_to(root))
    plan = plan_de_novo_tool(root, relative, require_inputs=True)
    return {"state": "PLAN_COMPILED" if plan.get("status") == "PASS" else "NEEDS_INPUTS",
        "intake": {"state": "VALIDATED", "intent": "de-novo"}, "route_plan": plan,
        "request_path": relative, "request": fresh.model_dump(mode="json"),
        "source_request_path": str(source.relative_to(root)), "source_sha256": hashlib.sha256(raw).hexdigest(),
        "next_call": None, "compute_submitted": False, "historical_candidates_reused": False,
        "note": "Plan compilation only; inspect the actual plan decision, asset and approval gates. No generation submitted."}
