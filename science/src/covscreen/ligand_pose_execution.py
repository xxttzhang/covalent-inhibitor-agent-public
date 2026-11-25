"""Workflow lifecycle over the existing registered CarsiDock executor."""
import json
import csv

from rdkit import Chem

from .jobs import JobSubmissionRequest
from .ligand_design import design_folder, inspect
from .ligand_first_inputs import load_campaign
from .ligand_parent_evidence import require_latest
from .target_hypothesis import digest, folder, read


def bound_request(root, design_id):
    """Revalidate lineage and the fixed pilot policy before runtime compilation."""
    from .docking_policy import require_engine_enabled
    require_engine_enabled("carsidock-cov")
    current = inspect(root, design_id)
    if current["state"] != "POSE_PLAN_READY":
        raise ValueError("Pose execution requires an unblocked exported pose plan")
    require_latest(root, current["evidence_id"])
    read(root, current["evidence_id"])
    directory = design_folder(root, design_id)
    design = json.loads((directory / "design.json").read_text())
    plan = directory / "pose-plan"
    from .ligand_first_geometry import parse_geometry_policy
    policy = json.loads((plan / "geometry-policy.json").read_text())
    if parse_geometry_policy(policy).model_dump(mode="json") != policy:
        raise ValueError("Frozen geometry policy omits current criteria; reconsider and prepare a new branch")
    binding = json.loads((plan / "input-binding.json").read_text())
    if (digest(folder(root, current["evidence_id"]) / "manifest.json") != design["evidence_manifest_sha256"]
            or binding["design_id"] != design_id
            or binding["design_manifest_sha256"] != digest(directory / "manifest.json")
            or binding["generation_manifest_sha256"] != digest(directory / "generation/manifest.json")):
        raise ValueError("Pose plan source lineage changed")
    records = json.loads((directory / "generation/candidates.json").read_text())["candidates"]
    selected = [record["candidate_id"] for record in records[:3]]
    if (not selected or binding["selected_candidate_ids"] != selected
            or binding["deferred_candidate_ids"] != [record["candidate_id"] for record in records[3:]]):
        raise ValueError("Pose plan candidate selection changed")
    from .rdkit_covalentizer import WARHEAD_ATOM_MAPS
    with (plan / "ligands.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    expected_rows = [{"input_sdf_index": str(index), "compound_id": candidate,
                      "reaction_type": "Michael Addition",
                      "electrophilic_atom_map": str(WARHEAD_ATOM_MAPS["reaction_beta_carbon"])}
                     for index, candidate in enumerate(selected)]
    exported_molecules = list(Chem.SDMolSupplier(str(plan / "ligands.sdf"), removeHs=False))
    originals = list(Chem.SDMolSupplier(str(directory / "generation/candidates.sdf"), removeHs=False))
    if rows != expected_rows or len(exported_molecules) != len(selected):
        raise ValueError("Pose plan candidate table changed")
    for index, molecule in enumerate(exported_molecules):
        original = originals[index]
        if original is not None:
            original.SetProp("_Name", selected[index])
        if (molecule is None or original is None
                or not molecule.HasProp("candidate_id")
                or molecule.GetProp("candidate_id") != selected[index]
                or not molecule.HasProp("compound_id")
                or molecule.GetProp("compound_id") != selected[index]
                or not molecule.HasProp("COMPOUND_ID")
                or molecule.GetProp("COMPOUND_ID") != selected[index]
                or not molecule.HasProp("_Name")
                or molecule.GetProp("_Name") != selected[index]
                or not molecule.HasProp("INPUT_SDF_INDEX")
                or molecule.GetIntProp("INPUT_SDF_INDEX") != index
                or Chem.MolToMolBlock(molecule) != Chem.MolToMolBlock(original)):
            raise ValueError("Pose plan candidate structure changed")
    campaign = load_campaign(directory / "case/campaign.json")
    relative = lambda path: str(path.relative_to(root))
    parameters = {
        "pdb": relative(directory / "case" / campaign.parent_pose.structure_path),
        "reference_ligand": relative(directory / "case/parent_pose.sdf"),
        "ligand_sdf": relative(plan / "ligands.sdf"), "ligand_table": relative(plan / "ligands.csv"),
        "covres": f"{campaign.parent_pose.protein_chain_id}:{campaign.target.nucleophile.residue_name}:{campaign.parent_pose.target_author_residue_number}",
        "reaction_type_column": "reaction_type", "num_conformer": 3, "seed": 20260725,
        "dispatch_policy": "ligand-smoke-v1", "dispatch_batch": "ligand-smoke-" + design_id,
        "input_row_count": len(selected),
    }
    if binding["source_inputs"] != {parameters[key]: digest(root / parameters[key])
                                   for key in ("pdb", "reference_ligand")}:
        raise ValueError("Pose plan source structures changed")
    expected = JobSubmissionRequest(request_id=design_id + "-pose-v1", project_id=design_id,
        route="ligand-first", engine="carsidock-cov", parameters=parameters,
        resource_budget={"max_runtime_minutes": 30, "gpu_count": 1, "cpu_count": 20,
                         "memory_gb": 200, "max_candidates": len(selected)}, execution_mode="plan")
    exported = JobSubmissionRequest.model_validate_json((plan / "job-request.json").read_text())
    if exported != expected:
        raise ValueError("Pose request differs from the bound pilot policy")
    # Existing executor includes this binding in its hashed scientific inputs.
    return expected.model_copy(update={"parameters": {
        **parameters, "docking_binding": relative(plan / "input-binding.json")}})


def check_runtime(root, design_id, *, request_id=None):
    """Compile through the real runtime boundary; no registration or GPU launch."""
    from .execution import JobManager
    from .approval_scope import normalize_spec
    request = bound_request(root, design_id)
    if request_id is not None:
        request = request.model_copy(update={"request_id": request_id})
    spec = JobManager(root)._prepare_submission(request)
    return {"state": "POSE_PLAN_READY", "design_id": design_id,
            "runtime_check": "registered_plan_compiled", "runtime_registration": "NOT_RUN",
            "compute_submitted": False, "approval": "NOT_REQUESTED",
            "compiled_spec": normalize_spec(spec),
            "limitations": ["Compilation does not test GPU availability or execute docking.",
                           "Pose geometry review does not replace independent refinement or MD."]}


def freeze_runtime(root, state):
    """Persist one request identity and exact compiled inputs in workflow state."""
    if (state["stage"] != "POSE_PLAN_READY"
            or inspect(root, state["design_id"])["evidence_id"] != state["evidence_id"]):
        raise ValueError("Pose runtime plan requires the current workflow design and evidence")
    previous = state.get("pose_execution")
    request_id = (previous["request_id"] if previous else
                  state["workflow_id"] + "-pose-r" + str(state["revision"] + 1))
    result = check_runtime(root, state["design_id"], request_id=request_id)
    directory = design_folder(root, state["design_id"])
    manifests = [directory / "manifest.json", directory / "generation/manifest.json",
                 directory / "pose-plan/manifest.json",
                 folder(root, state["evidence_id"]) / "manifest.json"]
    record = {"request_id": request_id, "design_id": state["design_id"],
              "evidence_id": state["evidence_id"], "spec": result["compiled_spec"],
              "source_manifests": {str(path.relative_to(root)): digest(path) for path in manifests}}
    if previous is not None and record != previous:
        raise ValueError("Frozen pose runtime plan changed; reconsider before planning a new branch")
    state["pose_execution"] = record
    return {**result, "runtime_plan_frozen": True, "request_id": request_id}


def frozen_request(root, state):
    """Recompile the current sources before any future approval or submission."""
    if state.get("pending"):
        raise ValueError("Reconcile the pending workflow operation before using the pose plan")
    if not state.get("pose_execution"):
        raise ValueError("Check and freeze the pose runtime plan first")
    freeze_runtime(root, state)
    return bound_request(root, state["design_id"]).model_copy(
        update={"request_id": state["pose_execution"]["request_id"]})


def execution_request(root, state, arguments):
    request = frozen_request(root, state)
    return JobSubmissionRequest.model_validate({**request.model_dump(mode="json"),
        "execution_mode": "execute", "human_approved": arguments.get("human_approved", False),
        "approval_note": arguments.get("approval_note")})


def start_request(root, arguments):
    from .ligand_workflow import directory
    from .ligand_parent_evidence import version_lock
    from .workflow_store import load
    from .workflow_protocol import revision
    location = directory(root, arguments["workflow_id"])
    with version_lock(location, ".workflow.lock"):
        state = load(location)
        revision(state, arguments["expected_revision"])
        if (arguments.get("action") != "start_pose" or set(arguments) - {
                "action", "workflow_id", "expected_revision", "human_approved", "approval_note"}):
            raise ValueError("Pose execution accepts only the frozen workflow plan")
        return execution_request(root, state, arguments)


def observe(root, state, run_id):
    from .execution import JobManager
    from .approval_scope import normalize_spec
    from .native_approval import verify_spec, decode
    manager = JobManager(root)
    spec = manager.registry.load_spec(run_id)
    result = manager.registry.load_result(run_id)
    record = state["pose_execution"]
    verify_spec(spec)
    scope = record["approved_scope"]
    if (spec.request_id != record["request_id"] or spec.project_id != record["design_id"]
            or spec.route != "ligand-first" or spec.engine != "carsidock-cov"
            or normalize_spec(spec) != scope["spec"]
            or decode(decode(spec.provenance["native_approval"])["approval"])["scope"] != scope
            or any(getattr(result, key) != getattr(spec, key)
                   for key in ("run_id", "project_id", "route", "engine", "attempt", "parent_run_id"))):
        raise ValueError("Pose job differs from the exact approved workflow request")
    for name, expected in {**record["source_manifests"], **spec.input_sha256}.items():
        path = root / name
        if not path.is_file() or path.is_symlink() or digest(path) != expected:
            raise ValueError("Pose job frozen input changed")
    record.update(run_id=run_id, job_state=result.state, return_code=result.return_code)
    state["stage"] = "POSE_EXECUTION_REVIEW" if result.state in {
        "COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"} else "POSE_EXECUTION_RUNNING"
    return {"run_id": run_id, "job_state": result.state, "scientific_result_verified": False,
            "output_validation": "NOT_EVALUATED"}


def lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response, DESIGN
    from .ligand_parent_evidence import version_lock
    from .workflow_store import load
    from .workflow_protocol import revision, begin_operation, changed
    from .native_approval import CURRENT, NativeApprovalDenied, decode
    from .execution import JobManager
    from .submission_identity import lookup_submission
    location = directory(root, workflow_id)
    with version_lock(location, ".workflow.lock"):
        state = load(location)
        revision(state, expected_revision)
        if action == "start_pose":
            if not CURRENT.get():
                raise NativeApprovalDenied("Pose execution requires exact native approval")
            if set(arguments) - {"human_approved", "approval_note"}:
                raise ValueError("Workflow owns the pose request identity")
            request = execution_request(root, state, arguments)
            state["pose_execution"]["approved_scope"] = decode(CURRENT.get())["scope"]
            begin_operation(location, state, DESIGN, action, submission={
                "project_id": request.project_id, "request_id": request.request_id, "execution_mode": "execute"})
            # Persist before submit. Any exception retains the original identity.
            result = JobManager(root).submit(request)
            output = observe(root, state, result.run_id)
        elif action == "reconcile":
            pending = state.get("pending")
            if arguments or not pending or pending.get("action") != "start_pose":
                raise ValueError("No uncertain pose submission to reconcile")
            output = lookup_submission(root, pending["submission"])
            if not output.get("registered_job_observed"):
                return response(state, output)
            output = observe(root, state, output["run_id"])
        elif action in {"pose_status", "evaluate_pose", "prepare_refinement", "prepare_refinement_target", "report_pose"}:
            if arguments or state.get("pending") or state["stage"] not in {
                    "POSE_EXECUTION_RUNNING", "POSE_EXECUTION_REVIEW"}:
                raise ValueError("Observe only the current registered pose job")
            record = state["pose_execution"]
            try:
                output = observe(root, state, record["run_id"])
            except (ValueError, KeyError, OSError, TypeError):
                record["evaluation_verification"] = "evidence_rejected"
                state["refinement_input"] = None
                state["pose_report"] = None
                changed(location, state, "pose_evidence_rejected")
                raise
            if action in {"evaluate_pose", "prepare_refinement", "prepare_refinement_target", "report_pose"} or record.get("evaluation"):
                from .ligand_pose_evidence import evaluate_outputs
                record["evaluation_verification"] = "evidence_rejected"
                manager = JobManager(root)
                try:
                    evaluation = evaluate_outputs(root, state, manager,
                        manager.registry.load_spec(record["run_id"]), manager.registry.load_result(record["run_id"]))
                except (ValueError, KeyError, OSError, TypeError):
                    state["refinement_input"] = None
                    state["pose_report"] = None
                    changed(location, state, "pose_evidence_rejected")
                    raise
                if record.get("evaluation") != evaluation:
                    state["refinement_input"] = None
                    state["pose_report"] = None
                record.update(evaluation=evaluation, evaluation_verification="verified")
                output["evaluation"] = evaluation
                output["output_validation"] = "MAPPED_PRODUCT_GRAPH_AND_GEOMETRY_RECHECKED"
                output["scientific_result_verified"] = True
                if action == "prepare_refinement":
                    from .ligand_refinement_input import freeze_refinement_input
                    try:
                        require_latest(root, state["evidence_id"])
                        state["refinement_input"] = freeze_refinement_input(root, state)
                    except (ValueError, KeyError, OSError, TypeError):
                        state["refinement_input"] = None
                        changed(location, state, "refinement_input_rejected")
                        raise
                    output["refinement_input"] = state["refinement_input"]
                if action == "prepare_refinement_target":
                    from .ligand_refinement_target import freeze
                    try:
                        target = freeze(root, state)
                    except (ValueError, KeyError, OSError, TypeError):
                        if state.get("refinement_input"):
                            state["refinement_input"].pop("target", None)
                        changed(location, state, "refinement_target_rejected")
                        raise
                    state["refinement_input"]["target"] = target
                    state["refinement_input"].pop("target_error", None)
                    output["refinement_input"] = state["refinement_input"]
                from .ligand_refinement_target import revalidate_saved
                diagnosis = revalidate_saved(root, state)
                if diagnosis:
                    changed(location, state, "refinement_target_rejected", diagnosis)
                    raise ValueError("Frozen refinement target failed revalidation: " + diagnosis["reason"])
                if action == "report_pose" or state.get("pose_report"):
                    from .ligand_pose_report import freeze
                    try:
                        require_latest(root, state["evidence_id"])
                        state["pose_report"] = freeze(root, state)
                    except (ValueError, KeyError, OSError, TypeError):
                        state["pose_report"] = None
                        changed(location, state, "pose_report_rejected")
                        raise
                    output["pose_report"] = state["pose_report"]
        else:
            raise ValueError("Unknown pose lifecycle action")
        state["pending"] = None
        changed(location, state, action, {"job_state": output["job_state"]})
        return response(state, output)
