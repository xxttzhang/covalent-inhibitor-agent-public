"""Workflow lifecycle over the existing candidate preparation engines."""
from copy import deepcopy

from covscreen.approval_scope import normalize_spec
from covscreen.execution import JobManager
from covscreen.jobs import JobSpec, sha256_file
from covscreen.native_approval import CURRENT, NativeApprovalDenied, decode, verify_spec
from covscreen.workflow_protocol import begin_operation, changed, revision
from covscreen.workflow_store import load
from covscreen.ligand_parent_evidence import version_lock
from covscreen import tool_api as api
from .md_candidate_pose import load_candidate_pose_input

STAGES = {"admission": "library-first-md-adduct-admission", "parameterization": "library-first-adduct-parameterization",
    "system_preparation": "library-first-openmm-system-preparation", "short_qc": "library-first-openmm-short-qc",
    "equilibration": "library-first-openmm-equilibration"}
PARENTS = {"parameterization": "admission", "system_preparation": "parameterization", "short_qc": "system_preparation",
    "equilibration": "short_qc"}
PLAN_FIELDS = {"admission": ["resource_budget", "native_timeout_seconds"],
    "parameterization": ["resource_budget", "native_timeout_seconds"],
    "system_preparation": ["resource_budget", "max_minimization_iterations"],
    "short_qc": ["resource_budget", "steps", "report_interval", "velocity_seed"],
    "equilibration": ["resource_budget", "integrator_seed", "barostat_seed"]}
STARTS = {"start_md_" + stage for stage in STAGES}
ACTIONS = {name for stage in STAGES for name in ("plan_md_" + stage, "start_md_" + stage, "md_" + stage + "_status")} | {"cancel_md_stage", "release_md_stage", "retry_md_stage"}
FAILED_STATES = {"FAILED", "CANCELLED", "TIMED_OUT"}


def budget_constraint(name):
    equilibration = name == "equilibration"
    return {"type": "object", "properties": {
        "max_runtime_minutes": {"type": "integer", "minimum": 1, "maximum": 720 if equilibration else 60},
        "cpu_count": {"type": "integer", "minimum": 1, "maximum": 8 if equilibration else 4},
        "memory_gb": {"type": "number", "exclusiveMinimum": 0, "maximum": 32 if equilibration else 16},
        "gpu_count": {"type": "integer", "enum": [0, 1] if equilibration else [0]},
        "max_candidates": {"type": "integer", "const": 1}}}


def valid_budget(name, budget):
    """Check the published bounded scalar contract without an optional JSON-schema runtime."""
    import math
    for field, rule in budget_constraint(name)['properties'].items():
        value = budget.get(field)
        if (type(value) not in (int, float) or not math.isfinite(value)
                or rule['type'] == 'integer' and type(value) is not int):
            return False
        if ('minimum' in rule and value < rule['minimum']
                or 'maximum' in rule and value > rule['maximum']
                or 'exclusiveMinimum' in rule and value <= rule['exclusiveMinimum']
                or 'enum' in rule and value not in rule['enum']
                or 'const' in rule and value != rule['const']):
            return False
    return True


def stage(record):
    return record.get("stage", "admission")  # Read earlier admission records unchanged.


def actions(state):
    record = state["md_execution"]
    name = stage(record)
    if state["stage"] == "MD_" + name.upper() + "_PLAN_READY":
        return ["plan_md_" + name, "start_md_" + name, "release_md_stage"]
    values = ["md_" + name + "_status"]
    if record.get("job_state") in {"QUEUED", "RUNNING"} and not record.get("cancellation_requested"):
        values.append("cancel_md_stage")
    if (record.get("run_id") and record.get("job_state") in FAILED_STATES
            and record.get("verification") != "evidence_rejected"):
        values.append("retry_md_stage")
    if record.get("verification") == "verified":
        values.extend("plan_md_" + child for child, parent in PARENTS.items() if parent == name)
    return values


def parent_state(state):
    return {**state, "md_execution": state["md_execution"]["parent"]}


def parent_input(root, state, child):
    parent = parent_state(state) if stage(state["md_execution"]) == child else state
    record = parent["md_execution"]
    name = PARENTS[child]
    if stage(record) != name or not record.get("run_id"):
        raise ValueError(child + " requires the original completed " + name)
    record["verification"] = "evidence_rejected"
    manager, spec, result = verify_job(root, parent, record["run_id"])
    value = validator(name)(root, parent, manager, spec, result)
    record[name] = value
    record["verification"] = "verified"
    return value


def input_arguments(name, asset):
    if name == "admission":
        return {"candidate_pose_artifact_id": asset["artifact_id"], "manifest_sha256": asset["manifest_sha256"]}
    if name == "parameterization":
        return {"adduct_admission_run_id": asset["run_id"], "adduct_input_manifest_sha256": asset["adduct_input_manifest_sha256"]}
    if name == "system_preparation":
        return {"parameterization_run_id": asset["run_id"], "parameterization_audit_sha256": asset["manifest_sha256"]}
    if name == "short_qc":
        return {"system_preparation_run_id": asset["run_id"], "preparation_manifest_sha256": asset["manifest_sha256"]}
    return {"short_qc_run_id": asset["run_id"], "short_qc_manifest_sha256": asset["manifest_sha256"]}


def factory(name):
    return {"admission": api._md_adduct_admission_request, "parameterization": api._adduct_parameterization_request,
        "system_preparation": api._openmm_system_preparation_request, "short_qc": api._openmm_short_qc_request,
        "equilibration": api._openmm_equilibration_request}[name]


def validator(name):
    return {"admission": admission, "parameterization": parameterization, "system_preparation": system_preparation,
        "short_qc": short_qc, "equilibration": equilibration}[name]


def bound_input(root, state):
    if state.get('historical_md_source'):
        from .historical_md import validate
        return validate(root, state, candidate=True)
    asset = state.get("md_input")
    choice = state.get("md_candidate")
    if not asset or not choice or asset["choice_id"] != choice["id"]:
        raise ValueError("Native graph admission requires the frozen selected candidate")
    loaded = load_candidate_pose_input(root, asset["artifact_id"], asset["manifest_sha256"])
    manifest = loaded["manifest"]
    if (manifest["compound_id"] != choice["candidate"]["compound_id"]
            or manifest["input_sdf_index"] != choice["candidate"]["input_sdf_index"]):
        raise ValueError("Frozen pose differs from the selected candidate")
    return loaded


def request(root, state, args, *, mode="execute"):
    bound_input(root, state)
    plan = state["md_execution"]["plan"]
    name = stage(state["md_execution"])
    asset = state["md_input"] if name == "admission" else parent_input(root, state, name)
    if plan["input"] != asset:
        raise ValueError("MD stage plan input changed")
    inputs = input_arguments(name, asset)
    values = {**plan["arguments"], **inputs,
        "human_approved": args.get("human_approved", False), "approval_note": args.get("approval_note")}
    value = factory(name)(root, values, execution_mode=mode).model_copy(
        update={"request_id": plan["request_id"], "attempt": plan["spec"].get("attempt", 1)})
    # A new approval must describe the saved plan, not silently adopt a changed
    # registered runtime between planning and requesting native confirmation.
    planned = value.model_copy(update={"execution_mode": "plan", "human_approved": False, "approval_note": None})
    if normalize_spec(JobManager(root).build_spec(planned)) != normalize_spec(JobSpec.model_validate(plan["spec"])):
        raise ValueError("Frozen MD stage plan changed; replan before requesting execution approval")
    return value


def start_request(root, args):
    from .workflow import directory
    folder = directory(root, args["workflow_id"])
    with version_lock(folder, ".workflow.lock"):
        state = load(folder)
        revision(state, args["expected_revision"])
        name = stage(state["md_execution"])
        if (args.get("action") != "start_md_" + name or state["stage"] != "MD_" + name.upper() + "_PLAN_READY"
                or state["pending"] or set(args) - {"action", "workflow_id", "expected_revision", "human_approved", "approval_note"}):
            raise ValueError("MD stage requires the current frozen workflow plan")
        return request(root, state, args)


def verify_job(root, state, run_id, *, check_inputs=True):
    manager = JobManager(root)
    spec = manager.registry.load_spec(run_id)
    result = manager.registry.load_result(run_id)
    verify_spec(spec)
    approved = state["md_execution"]["approved_scope"]
    plan = state["md_execution"]["plan"]
    proof = decode(spec.provenance["native_approval"])
    name = stage(state["md_execution"])
    project = plan["input"]["artifact_id"] if name == "admission" else plan["input"]["run_id"]
    if (spec.engine != STAGES[name] or spec.request_id != plan["request_id"]
            or spec.project_id != project or spec.route != "library-first"
            or name != "admission" and spec.parent_run_id != project
            or normalize_spec(spec) != approved["spec"] or decode(proof["approval"])["scope"] != approved
            or any(getattr(result, key) != getattr(spec, key)
                   for key in ("run_id", "project_id", "engine", "route", "attempt", "parent_run_id"))):
        raise ValueError("MD stage job differs from exact workflow approval")
    if not check_inputs:
        return manager, spec, result
    bound_input(root, state)
    if name != "admission" and plan["input"] != parent_input(root, state, name):
        raise ValueError("MD stage parent changed")
    for name, digest in spec.input_sha256.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise ValueError("MD stage frozen input changed")
    return manager, spec, result


def admission(root, state, manager, spec, result):
    from covscreen.md_graph_evidence import graph_admission_receipt
    bound_input(root, state)
    return graph_admission_receipt(root, state["md_execution"], manager, spec, result, state["md_input"])


def parameterization(root, state, manager, spec, result):
    from covscreen.md_parameterization_evidence import parameterization_receipt
    parent = parent_input(root, state, "parameterization")
    return parameterization_receipt(root, state["md_execution"], manager, spec, result, parent)


def system_preparation(root, state, manager, spec, result):
    from covscreen.md_preparation_evidence import preparation_receipt
    parent = parent_input(root, state, "system_preparation")
    return preparation_receipt(root, state["md_execution"], manager, spec, result, parent)


def short_qc(root, state, manager, spec, result):
    from covscreen.md_short_qc_evidence import short_qc_receipt
    parent = parent_input(root, state, "short_qc")
    return short_qc_receipt(root, state["md_execution"], manager, spec, result, parent)


def equilibration(root, state, manager, spec, result):
    from covscreen.md_equilibration_evidence import equilibration_receipt
    parent = parent_input(root, state, "equilibration")
    return equilibration_receipt(root, state["md_execution"], manager, spec, result, parent)


def observe(root, state, run_id):
    from covscreen.worker_observation import observe_worker
    record = state["md_execution"]
    record["verification"] = "evidence_rejected"
    try:
        manager, spec, result = verify_job(root, state, run_id)
    except (ValueError, KeyError, OSError, TypeError) as error:
        record["error"] = type(error).__name__ + ": " + str(error)
        raise
    record.update(run_id=run_id, job_state=result.state, verification="not_ready",
        return_code=result.return_code, failure_reason=result.failure_reason, message=result.message,
        worker=observe_worker(manager.registry, spec, result))
    record.pop("error", None)
    name = stage(record)
    prefix = "MD_" + name.upper()
    from covscreen.job_cancellation import requested
    record["cancellation_requested"] = requested(manager.registry, run_id)
    state["stage"] = prefix + "_EXECUTION"
    if result.state in {"FAILED", "CANCELLED", "TIMED_OUT"}:
        state["stage"] = prefix + "_FAILED"
    if result.state == "COMPLETED":
        # Invalid completed evidence stays visibly rejected; never retain a stale success.
        record["verification"] = "evidence_rejected"
        state["stage"] = prefix + "_REVIEW"
        try:
            record[name] = validator(name)(root, state, manager, spec, result)
        except (ValueError, KeyError, OSError, TypeError) as error:
            record["error"] = type(error).__name__ + ": " + str(error)
        else:
            record["verification"] = "verified"
            record.pop("error", None)
    return public(state)


def public(state):
    record = state.get("md_execution")
    if not record:
        return None
    name = stage(record)
    verified = record.get("verification") == "verified"
    return {"stage": "native_graph_admission" if name == "admission" else name, "request_id": record["plan"]["request_id"],
        "attempt": record["plan"]["spec"].get("attempt", 1),
        "previous_attempts": [{"run_id": old["run_id"], "attempt": old["plan"]["spec"].get("attempt", 1),
            "job_state": old.get("job_state"), "return_code": old.get("return_code"),
            "failure_reason": old.get("failure_reason"), "message": old.get("message")}
            for old in submitted_attempts(state, name, record["plan"]["input"], archived_only=True)],
        "retry_plan_error": record.get("retry_plan_error"),
        "input": deepcopy(record["plan"]["input"]), "resource_budget": record["plan"]["spec"]["resource_budget"],
        "run_id": record.get("run_id"), "job_state": record.get("job_state", "PLANNED"),
        "return_code": record.get("return_code"), "failure_reason": record.get("failure_reason"),
        "message": record.get("message"),
        "evidence_error": record.get("error") if record.get("verification") == "evidence_rejected" else None,
        "verification": record.get("verification", "not_ready"), "worker": record.get("worker"),
        "admission": deepcopy(record.get("admission")) if name == "admission" and verified else None,
        "parameterization": deepcopy(record.get("parameterization")) if name == "parameterization" and verified else None,
        "system_preparation": deepcopy(record.get("system_preparation")) if name == "system_preparation" and verified else None,
        "short_qc": deepcopy(record.get("short_qc")) if name == "short_qc" and verified else None,
        "equilibration": deepcopy(record.get("equilibration")) if name == "equilibration" and verified else None,
        "cancellation_requested": record.get("cancellation_requested", False),
        "cancellation_confirmed": record.get("job_state") == "CANCELLED",
        "parameterization_completed": name != "admission" and verified,
        "system_preparation_completed": name in {"system_preparation", "short_qc", "equilibration"} and verified,
        "short_qc_completed": name in {"short_qc", "equilibration"} and verified,
        "equilibration_protocol_completed": name == "equilibration" and verified,
        "production_review_ready": name == "equilibration" and verified and record["equilibration"]["production_review_ready"],
        "md_system_ready": False}


def submitted_attempts(state, name, asset, *, archived_only=False):
    """Use retained original jobs, never count unsubmitted replacement drafts."""
    records = [branch.get("md_execution") for branch in state.get("branches", [])]
    if not archived_only:
        records.append(state.get("md_execution"))
    return [record for record in records if record and record.get("run_id")
            and stage(record) == name and record["plan"]["input"] == asset]


def compile_plan(root, state, name, arguments, *, retry_reason=None):
    """Compile first; only a fully checked replacement changes workflow state."""
    bound_input(root, state)
    previous = state.get("md_execution")
    same_stage = previous and stage(previous) == name
    if same_stage and previous.get("run_id") and retry_reason is None:
        raise ValueError("An executed MD stage requires explicit terminal retry planning")
    asset = state["md_input"] if name == "admission" else parent_input(root, state, name)
    attempt = max((record["plan"]["spec"].get("attempt", 1)
                   for record in submitted_attempts(state, name, asset)), default=0) + 1
    compiled = factory(name)(root, {**arguments, **input_arguments(name, asset)}, execution_mode="plan").model_copy(
        update={"request_id": state["workflow_id"] + "-md-" + name + "-r" + str(state["revision"] + 1),
                "attempt": attempt})
    if (not valid_budget(name, compiled.resource_budget.model_dump(mode="json"))
            or any(type(compiled.parameters[key]) is not int for key in PLAN_FIELDS[name] if key != "resource_budget")
            or name == "system_preparation" and not 1 <= compiled.parameters["max_minimization_iterations"] <= 1000):
        raise ValueError("MD stage exceeds its single-candidate resource contract")
    spec = JobManager(root).build_spec(compiled)
    if same_stage:
        state["branches"].append({"revision": state["revision"], "stage": state["stage"],
            "reason": retry_reason or "Superseded unsubmitted MD stage plan", "md_execution": deepcopy(previous)})
    parent = previous.get("parent") if same_stage else previous
    state["md_execution"] = {"stage": name, **({"parent": deepcopy(parent)} if name != "admission" else {}),
        "plan": {"input": deepcopy(asset), "arguments": deepcopy(arguments),
                 "request_id": compiled.request_id, "spec": spec.model_dump(mode="json")}}
    state["stage"] = "MD_" + name.upper() + "_PLAN_READY"


def retry_plan(root, folder, state, reason):
    if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 2000:
        raise ValueError("Terminal MD retry planning requires a concrete reason of 20 to 2000 characters")
    observed = deepcopy(state)
    try:
        record = observed["md_execution"]
        if observed.get("pending") or not record.get("run_id"):
            raise ValueError("Reconcile the original MD operation before retry planning")
        observe(root, observed, record["run_id"])
        if record.get("job_state") not in FAILED_STATES:
            raise ValueError("Retry planning requires a freshly verified FAILED, TIMED_OUT or CANCELLED job")
        proposed = deepcopy(observed)
        compile_plan(root, proposed, stage(record), record["plan"]["arguments"], retry_reason=reason.strip())
    except (ValueError, KeyError, OSError, TypeError) as error:
        # Failed revalidation/compilation preserves the original task and its
        # latest diagnostics; no archive, pending submission or new job exists.
        observed["md_execution"]["retry_plan_error"] = type(error).__name__ + ": " + str(error)
        state.clear()
        state.update(observed)
        changed(folder, state, "md_retry_plan_rejected")
        raise
    state.clear()
    state.update(proposed)


def cancellation_scope(root, args):
    from .workflow import directory, view, RESEARCH
    from covscreen.job_cancellation import validate_request
    folder = directory(root, args["workflow_id"])
    with version_lock(folder, ".workflow.lock"):
        state = load(folder)
        revision(state, args["expected_revision"])
        if (args.get("action") != "cancel_md_stage" or state["pending"]
                or "cancel_md_stage" not in view(state)["tools"].get(RESEARCH, {}).get("actions", [])
                or set(args) - {"action", "workflow_id", "expected_revision", "human_approved", "approval_note"}):
            raise ValueError("Cancellation requires the active workflow's exact job")
        run = state["md_execution"]["run_id"]
        manager, _, _ = verify_job(root, state, run, check_inputs=False)
        validate_request(manager.registry, run)
        return {"kind": "cancel", "run_id": run, "spec_sha256": sha256_file(manager.registry.job_dir(run) / "spec.json")}


def reconcile(root, state, pending):
    if pending["action"] == "cancel_md_stage":
        from covscreen.job_cancellation import requested
        run = state["md_execution"]["run_id"]
        manager, _, result = verify_job(root, state, run, check_inputs=False)
        observed = requested(manager.registry, run)
        state["md_execution"].update(cancellation_requested=observed, job_state=result.state,
            return_code=result.return_code, failure_reason=result.failure_reason, message=result.message)
        if result.state in {"CANCELLED", "FAILED", "TIMED_OUT"}:
            state["stage"] = "MD_" + stage(state["md_execution"]).upper() + "_FAILED"
        if observed or result.state not in {"QUEUED", "RUNNING"}:
            state["pending"] = None
        return {"run_id": run, "cancellation_requested": observed, "cancellation_confirmed": result.state == "CANCELLED",
            "automatic_resubmission_allowed": False}
    from covscreen.submission_identity import lookup_submission
    found = lookup_submission(root, pending["submission"])
    if found.get("registered_job_observed"):
        observe(root, state, found["run_id"])
        state["pending"] = None
    return found


def step(root, tool, workflow_id, expected_revision, action, args):
    from .workflow import RESEARCH, directory, view, response
    folder = directory(root, workflow_id)
    with version_lock(folder, ".workflow.lock"):
        state = load(folder)
        revision(state, expected_revision)
        if tool != RESEARCH or action not in view(state)["tools"].get(tool, {}).get("actions", []):
            raise ValueError("MD stage action unavailable at current stage")
        planning = action.startswith("plan_md_")
        starting = action in STARTS
        allowed = set(PLAN_FIELDS[action.removeprefix("plan_md_")]) if planning else {
            "human_approved", "approval_note"} if starting or action == "cancel_md_stage" else {"reason"} if action in {"release_md_stage", "retry_md_stage"} else set()
        if set(args) - allowed:
            raise ValueError("Workflow owns candidate, task and input identity")
        if planning:
            name = action.removeprefix("plan_md_")
            bound_input(root, state)
            previous = state.get("md_execution")
            try:
                asset = state["md_input"] if name == "admission" else parent_input(root, state, name)
            except (ValueError, KeyError, OSError, TypeError):
                if previous:
                    previous["verification"] = "evidence_rejected"
                    changed(folder, state, "md_parent_evidence_rejected")
                raise
            compile_plan(root, state, name, args)
        elif action == "retry_md_stage":
            retry_plan(root, folder, state, args.get("reason"))
        elif starting:
            if not CURRENT.get():
                raise NativeApprovalDenied("MD stage requires exact native approval")
            compiled = request(root, state, args)
            state["md_execution"]["approved_scope"] = decode(CURRENT.get())["scope"]
            begin_operation(folder, state, tool, action, submission={"project_id": compiled.project_id,
                "request_id": compiled.request_id, "execution_mode": "execute"})
            # On any submit/readback error the durable pending identity survives.
            result = JobManager(root).submit(compiled)
            observe(root, state, result.run_id)
            state["pending"] = None
        elif action == "release_md_stage":
            record = state["md_execution"]
            reason = args.get("reason")
            if (record.get("run_id") or record.get("approved_scope") or not isinstance(reason, str)
                    or not 20 <= len(reason.strip()) <= 2000):
                raise ValueError("Only an unapproved unsubmitted MD plan can be released with a concrete reason")
            state["branches"].append({"revision": state["revision"], "stage": state["stage"], "reason": reason, "md_execution": deepcopy(record)})
            state["md_execution"] = record.get("parent")
            state["stage"] = "MD_" + stage(state["md_execution"]).upper() + "_REVIEW" if state["md_execution"] else (
                'HISTORICAL_MD_SOURCE_REVIEW' if state.get('historical_md_source') else "REFINEMENT_CANDIDATE_REVIEW")
        elif action == "cancel_md_stage":
            from covscreen.job_cancellation import validate_request
            if not CURRENT.get():
                raise NativeApprovalDenied("Stage cancellation requires exact native approval")
            record = state["md_execution"]
            run = record["run_id"]
            manager, _, _ = verify_job(root, state, run, check_inputs=False)
            validate_request(manager.registry, run)
            scope = {"kind": "cancel", "run_id": run, "spec_sha256": sha256_file(manager.registry.job_dir(run) / "spec.json")}
            if decode(CURRENT.get())["scope"] != scope:
                raise ValueError("Cancellation approval differs from the exact workflow job")
            record["cancellation_scope"] = scope
            begin_operation(folder, state, tool, action)
            api.cancel_job_tool(root, run, human_approved=args.get("human_approved", False))
            reconcile(root, state, state["pending"])
        else:
            try:
                observe(root, state, state["md_execution"]["run_id"])
            except (ValueError, KeyError, OSError, TypeError):
                changed(folder, state, "md_admission_evidence_rejected")
                raise
        changed(folder, state, action)
        return response(state)
