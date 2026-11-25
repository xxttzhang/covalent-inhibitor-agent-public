"""Thin MCP contract for the canonical Library-first workflow."""
from copy import deepcopy

from .annotations import EXECUTION
from .library_first import TOOLS as EXISTING
from ..library_workflow import ENTRY, RESEARCH, RECEPTOR, INTERFACE, navigate, step
from ..workflow_protocol import output_schema
from covscreen_library_first.screening_inputs import PROFILE
from covscreen_library_first.screening_plan import REVIEW_FIELDS
from covscreen_library_first.workflow_actions import RESEARCH_ACTIONS, RECEPTOR_ACTIONS
from covscreen_library_first.screening_results import SelectionPolicy
from ..receptor_environment_review import REVIEW_SCHEMA, EVIDENCE_READ_SCHEMA, READ_GUIDANCE

IDENTITY = {"workflow_id": {"type": "string", "pattern": INTERFACE["id_pattern"]},
            "expected_revision": {"type": "integer", "minimum": 1}}


def properties(name):
    return deepcopy(next(tool["inputSchema"]["properties"] for tool in EXISTING if tool["name"] == name))


research = {**properties("covalent_library_first_discover_structures"),
            **properties("covalent_library_first_acquire_structure"),
            **{k: v for k, v in properties("covalent_library_first_prepare_receptor").items()
               if k in {"receptor_input_artifact_id", "manifest_sha256"}}}
research.pop("cys", None)
research["action"] = {"type": "string", "enum": list(RESEARCH_ACTIONS)}
research.update({key: value for key, value in properties("covalent_library_first_admit_md_candidate").items()
                 if key in {"resource_budget", "native_timeout_seconds"}})
equilibration = properties("covalent_library_first_equilibrate_openmm_system")
research["resource_budget"] = {"anyOf": [research["resource_budget"], equilibration["resource_budget"]]}
research.update({key: equilibration[key] for key in ("integrator_seed", "barostat_seed")})
research["max_minimization_iterations"] = {"type": "integer", "minimum": 1, "maximum": 1000}
research.update({key: value for key, value in properties("covalent_library_first_run_openmm_short_qc").items()
                 if key in {"steps", "report_interval", "velocity_seed"}})
research["reason"] = {"type": "string", "minLength": 5, "maxLength": 2000}
research['candidate_ids'] = {'type': 'array', 'minItems': 1, 'maxItems': 5, 'uniqueItems': True,
    'items': {'type': 'string', 'minLength': 1}}
research["compound_id"] = {"type": "string", "minLength": 1, "maxLength": 256, "pattern": r"^[^\x00-\x1f\x7f]+$"}
research["input_sdf_index"] = {"type": "integer", "minimum": 0}
research['source_artifact_id'] = {'type': 'string', 'pattern': '^historical-md-source-[a-f0-9]{32}$'}
research['source_manifest_sha256'] = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
research["selection_policy"] = SelectionPolicy.model_json_schema()
research['environment_review'] = deepcopy(REVIEW_SCHEMA)
research.update(deepcopy(EVIDENCE_READ_SCHEMA))
for item in research['environment_review']['properties'].values():
    item['properties']['decision']['enum'] = ['suitable_for_exploratory_vs', 'suitable_for_exploratory_refinement', 'requires_revision', 'evidence_insufficient']
research["refinement_review"] = {"type": "string", "minLength": 20, "maxLength": 4000,
    "description": "Scientific rationale for refining the exact reviewed subset with the registered AF3/CovDock plan; does not approve compute."}
research["profile"] = {"type": "string", "enum": [PROFILE]}
research["scientific_review"] = {"type": "object", "properties": {
    name: {"type": "string", "minLength": 20, "maxLength": 4000} for name in REVIEW_FIELDS},
    "required": list(REVIEW_FIELDS), "additionalProperties": False}
research["max_points"] = {"type": "number", "minimum": 0,
    "description": "Proposed point budget for a later native approval; this value grants no resource permission."}
for key in ("target_artifact_id", "target_manifest_sha256"):
    research[key]["description"] = (
        "Optional reuse only: provide both an existing verified artifact ID and its actual manifest hash. "
        "Omit both to let resolve_target retrieve and persist the bound target's UniProt identity. Never invent either value."
    )
receptor = {k: v for k, v in properties("covalent_library_first_prepare_receptor").items()
            if k not in {"receptor_input_artifact_id", "manifest_sha256", "run_id"}}
receptor["action"] = {"type": "string", "enum": list(RECEPTOR_ACTIONS)}

TOOLS = [{"name": ENTRY,
    "description": "The Library-first research entry. Start with target/species, verify sequence identity, select a reasoned exploratory cysteine, then discover/acquire coordinates and prepare the receptor. State, artifact hashes and task identity are backend-owned. reconsider archives downstream assets; suspend changes session routing without cancelling jobs. Coordinate or receptor preparation is not screening readiness or biological validation. Host screening and MD workflow handoffs remain separately gated.",
    "inputSchema": {"type": "object", "properties": {**IDENTITY,
        "action": {"type": "string", "enum": ["start", "status", "resume", "suspend", "select_site", "reconsider", "inspect_branch", "reconcile"]},
        "request_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"},
        "target": {"type": "string"}, "taxon": {"type": "integer", "minimum": 1},
        "site": {"type": "string", "pattern": "^C[1-9][0-9]{0,4}$"},
        "reason": {"type": "string", "minLength": 5, "maxLength": 2000},
        "return_to": {"type": "string", "enum": ["target", "site", "structure"]},
        "branch_index": {"type": "integer", "minimum": 0}}, "required": ["action"], "additionalProperties": False},
    "annotations": EXECUTION}]
for name, props, description in [
    (RESEARCH, research, "Use only the current Library research stage. Target, selected cysteine and parent hashes bind automatically; choose structure entry/chain/model/altloc scientifically. Explicit artifact ID/hash pairs can reuse verified evidence; every reuse replays the raw parent chain. review_receptor replays existing local identity/geometry/context audits and freezes screening evidence; it may take minutes and submits no worker. screening_profiles lists supported input profiles; prepare_screening_inputs freezes the selected profile's actual library prefix, prepared receptor, registered same-coordinate reference and science configuration. This is data preparation only: currently a 20k/5 research pilot, not whole-library screening or scientific acceptance. read_screening_inputs validates the bound files. prepare_screening_plan records evidence-based rationale for construct and removed components, reaction limitations and prefix selection, plus a proposed point budget; read_screening_plan restores the same durable operation. compile_screening authenticates the host, locks this scientific plan and prepares one native unapproved draft; recover_screening restores the same operation after a lost reply. Neither compilation nor recovery grants permission. approve_screening uses the existing exact-plan native approval; cancel_screening requests cancellation and wake_screening resumes its original observer. screening_status reports verified persisted ledger evidence, not fresh remote checks or scientific acceptance. release_screening seals only an unapproved never-submitted operation before returning to planning; approved tasks require cancellation and reconciliation. migrate_screening_to_vs requires a reason and fresh host-ledger proof that the original task is cancelled with every stage still waiting and unsubmitted; it seals the old operation, retains receptor/input/approval history, and enters separate VS planning without granting or transferring compute approval. screening_report freshly verifies the completed task artifacts; select_screening_candidates requires an explicit selection_policy and a scientific reason of at least 20 characters, preserves denominators/exclusions/zero candidates, and creates only the existing immutable proposal. read_screening_candidates rereads the exact bound witness, never the latest unrelated proposal. File failures hide candidate results until reverified; ranking disagreement is not calibrated uncertainty, biological activity or refinement approval. Input paths and signing envelopes remain host-bound."),
    (RECEPTOR, receptor, "Prepare the active workflow's exact receptor. plan preserves deposited altloc choices and the existing runtime/quality gates. start requires native approval of its frozen workflow-scoped request; status recovers its task; assess_context produces descriptive measurements only. No model-supplied run ID, substituted receptor, implicit retries or MD."),
]:
    TOOLS.append({"name": name, "description": description,
        "inputSchema": {"type": "object", "properties": {**props, **IDENTITY},
            "required": ["action", "workflow_id", "expected_revision"], "additionalProperties": False}, "annotations": EXECUTION})
for tool in TOOLS:
    tool["outputSchema"] = output_schema(INTERFACE["route"], INTERFACE["protocol"], INTERFACE["id_pattern"], tuple(INTERFACE["tools"]))
TOOLS[1]["description"] = (
    "At TARGET_REVIEW call resolve_target with no evidence ID/hash to retrieve UniProt identity for the bound target/species; "
    "the server performs retrieval and saves the artifact. The host supplies workflow ID/revision. "
    "target_artifact_id and target_manifest_sha256 are an optional pair for reusing existing verified evidence, not prerequisites. "
    "Do not use shell or a separate web tool to manufacture these values. read_target rereads the saved identity. "
    "Identity and canonical cysteine enumeration do not establish residue domains, accessibility or chemical reactivity. "
    + TOOLS[1]["description"]
)
TOOLS[1]["description"] += " plan_refinement locks the reviewed nonempty subset, refinement_review and independent max_points before native planning. recover_refinement restores that same operation without reselection or implicit expiry renewal and reconciles an existing native execution. approve_refinement uses the exact native two-engine approval; refinement_status returns verified persisted ledger facts, not fresh remote artifact verification. cancel_refinement records cancellation of this exact task; wake_refinement resumes its existing observer only after approval. release_refinement seals planning only before activation or after an unapproved confirmation is cancelled with no execution records. Approved operations remain frozen. refinement_report freshly verifies the exact bound native report, preserving all candidate structural verdicts and measured values. It does not publish files, select MD candidates or validate affinity. When an independently approved delivery is incomplete, resume_refinement_delivery resumes only its existing file queue and observer, preserving uncertain saves and the exact workflow. It never reruns refinement or grants a new delivery policy. select_md_candidate refreshes the exact report and publications, then binds one explicit compound/index, scientific reason, native file versions and existing receptor parents. It records a decision only, never MD readiness or approval. release_md_candidate archives the unconsumed decision before replacement. prepare_md_input transfers only the exact selected native file versions through the authenticated host channel, resumes durable byte offsets and reuses the scientific pose freezer. Target residue and reaction are recovered from the exact approved native plan and must match the receiving receptor; missing candidate-table context is never guessed. It accepts no model file paths, submits no compute, and leaves native graph admission, parameterization and MD readiness pending."
TOOLS[0]["inputSchema"]["x-research-workflow"] = deepcopy(INTERFACE)
TOOLS[1]['description'] += ' After canonical target/site selection, attach_historical_md_source can bind an operator-frozen native Library source to an empty study. read_historical_md_source revalidates its retained provenance; prepare_historical_md_candidate checks the canonical construct and freezes the existing adduct for a NEW graph admission. No old job, approval or MD completion is adopted. Model file paths and caller-supplied source verification are not accepted.'
TOOLS[1]["description"] += " plan_md_admission binds the existing native graph admission engine to the frozen candidate pose and proposed resources. start_md_admission requires independent exact-plan native approval. md_admission_status verifies the original signed job and its outputs; reconcile recovers an uncertain start by exact submission identity without resubmission. Verified native graph evidence is not parameterization or MD readiness."
TOOLS[1]["description"] += " plan_vs_prescreen binds the current frozen 20000-record prefix, receptor/reference/configuration and explicit scientific_review to a new native CPU plan (0 GPU, 4 CPU, 16 GiB, 15 minutes, at most 5 shortlisted candidates). It does not read the mutable whole-library configuration or reuse old screening approval. start_vs_prescreen requires independent exact-plan approval; vs_prescreen_status verifies the original registered task and inputs. reconcile restores an uncertain registration without resubmitting. release_vs_prescreen requires a reason and is limited to an unapproved unregistered plan. Prescreen completion is not docking, accepted chemistry or MD readiness."
TOOLS[1]["description"] += " evaluate_vs_prescreen independently verifies registered source/target/selection manifests, counts and row coverage, candidate Parquet/CSV/SDF identity and unique Michael reactive atoms. It preserves zero candidates and failed quality gates without filling quotas. Eligible original candidates receive explicit alpha/beta maps for later separately approved VS; no docking is submitted. Status/resume revalidates saved evidence and hides summaries after file or task changes."
TOOLS[1]["description"] += " plan_md_parameterization freshly revalidates the bound admission outputs before planning the existing native parameterization engine. start_md_parameterization requires a separate exact-plan native approval; md_parameterization_status verifies both stages and registered output hashes. release_md_stage archives only an unapproved unsubmitted plan; cancel_md_stage requires approval for the exact active job and records cooperative cancellation, never claims termination from request acknowledgement. reconcile restores a lost cancellation receipt without relaunching. Verified parameterization does not imply system readiness, minimization or MD."


def handlers(root, arguments):
    return {ENTRY: lambda: navigate(root, **arguments), RESEARCH: lambda: step(root, RESEARCH, **arguments),
            RECEPTOR: lambda: step(root, RECEPTOR, **arguments)}

TOOLS[1]["description"] += " plan_md_system_preparation binds the verified parameterization to the existing OpenMM conversion, independent force-field audit and bounded minimization engine. start_md_system_preparation requires separate exact-plan approval. md_system_preparation_status checks registered outputs and all parent receipts; success leaves short QC, equilibration and MD readiness unfulfilled. Stage release, cancellation and reconcile use the same lifecycle."

TOOLS[1]["description"] += " plan_md_short_qc binds the verified preparation to the existing bounded CPU integration-stability check. start_md_short_qc requires independent exact-plan approval; md_short_qc_status validates registered final state, frame records, protocol and all parent receipts. Short QC is not equilibration, production readiness, affinity or kinetics validation."

TOOLS[1]["description"] += " plan_md_equilibration binds the verified short QC to the existing 500 ps NPT protocol and registered CPU/CUDA platform. Stage field_constraints narrow the published resource union. start_md_equilibration requires exact-plan approval. md_equilibration_status distinguishes completed diagnostic evidence from production-review admission; neither claims equilibrium convergence or starts production."

TOOLS[1]["description"] += " retry_md_stage freshly verifies the same signed FAILED, TIMED_OUT or CANCELLED task and all upstream evidence before preserving that failed attempt and freezing a new plan. It requires a reason, submits no computation and never reuses the previous approval. The new attempt requires its own exact-plan native approval; an uncertain or still-active task must be reconciled, not retried."

TOOLS[1]["description"] += " prepare_vs_input binds only the registered, freshly verified nonempty prescreen selection to the retained prepared receptor; no replacement candidate sampling or old approval transfer. read_vs_input returns the exact evidence inventory and canonical-to-author residue mapping. review_vs_environment addresses assembly, removed components, construct/site and reaction scope using source-bound citations. check_vs_runtime freezes a private CPU VS plan; configuration availability is not license checkout or compute approval. start_vs requires independent native approval of that exact plan and an enabled fixed release. vs_status observes the same registered task; reconcile recovers registration after a lost reply without resubmission. evaluate_vs re-reads registered raw Maestro products, verifies candidate/receptor/atom-lineage identity and evaluates the frozen Library-specific physical, local-strain and reference polar-contact-site gates. report_vs saves a content-addressed review with all candidates and missing evidence. No common scaffold or unrelated parent-strain subtraction is assumed. Changed products retract summaries on status/resume; restoring bytes observes the same job. Passing poses remain provisional review candidates, never automatic MD admission."
TOOLS[1]['description'] += ' prepare_vs_refinement freezes one to five explicit jointly passing candidate_ids and a reason of at least 20 characters from the current verified VS review. It retains original pre-reaction molecules, receptor and VS provenance for independent LO redocking. read_lo_input supplies the evidence inventory. review_lo_environment requires separate refinement judgments; a positive VS review is insufficient. check_lo_runtime freezes the private CPU LO plan (0 GPU, 4 CPU, 16 GiB, 180 minutes). start_lo requires new exact-plan native approval. lo_status and reconcile observe the original task without resubmission; evaluate_lo and report_lo validate registered LO products and preserve negative results. Neither VS selection nor LO completion grants MD admission.'
TOOLS[1]['description'] += ' read_vs_input and read_lo_input expose bound JSON and PDB headers. ' + READ_GUIDANCE
