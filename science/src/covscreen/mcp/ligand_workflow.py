"""Public workflow navigation and stage-scoped adapters; legacy APIs remain intact."""
from copy import deepcopy
from pathlib import Path

from .annotations import EXECUTION
from .upstream import TOOLS as UPSTREAM
from ..ligand_workflow import ENTRY, EVIDENCE, DESIGN, INTERFACE, navigate, step
from ..workflow_protocol import output_schema
from ..receptor_environment_review import REVIEW_SCHEMA

IDENTITY = {"workflow_id": {"type": "string", "pattern": "^ligand-flow-[a-f0-9]{24}$"},
            "expected_revision": {"type": "integer", "minimum": 1}}

TOOLS = [{"name": ENTRY,
    "description": "The single scientific entry for Ligand-first. start opens a durable workflow with explicit target/species and optional site; omit site when unresolved. No site opens target review: resolve_target verifies identity and canonical cysteines, then select_site records an exploratory hypothesis and reason, never validated reactivity. revise_target permits reasoned correction before collection. Optional evidence_id resumes verified site-bound evidence. status returns stage-visible tools; reconsider retains past branches and reopens parent comparison without relaxing evidence gates. Runtime computation requires separate registration/approval.",
    "inputSchema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["start", "status", "reconcile", "reconsider", "reopen_design", "inspect_branch", "select_site", "revise_target", "suspend", "resume", "attach_md_source"]},
        **IDENTITY, "request_id": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"},
        "target": {"type": "string"}, "site": {"type": "string", "pattern": "^C[1-9][0-9]{0,4}$"},
        "taxon": {"type": "integer", "minimum": 1},
        "evidence_id": {"type": "string", "pattern": "^hyp-[a-f0-9]{24}$"},
        "source_artifact_id": {"type": "string", "pattern": "^historical-md-source-[a-f0-9]{32}$"},
        "source_manifest_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "reason": {"type": "string", "minLength": 5, "maxLength": 2000},
        "branch_index": {"type": "integer", "minimum": 0}},
        "required": ["action"], "additionalProperties": False}, "annotations": EXECUTION}]
TOOLS[0]["description"] += " For open-ended research, autonomously choose a bounded exploratory hypothesis from verified canonical cysteines with a stated rationale; do not delegate research site/parent selection to the user. Prior knowledge guides hypotheses but is not verified evidence. Ask only for unresolved user intent/identity or missing user-owned constraints. suspend releases session routing without cancelling jobs; resume restores it."
TOOLS[0]["description"] += " reopen_design requires a nonempty reason alongside workflow_id and expected_revision; it resumes a reported positive design after revalidating its sealed inputs, preserving its delivery report and candidates. It discards only an unexecuted runtime plan so the executor must be checked again; it never resumes or resubmits an existing compute job."
TOOLS[0]["description"] += " After interruption, use reconcile only when exposed: local steps restore committed workflow assets without adopting partial output; background collection is observed by its durably reserved identity, never relaunched. Missing receipts remain outcome unknown. A recorded collection failure can then be reconsidered with a reason; it is not evidence of target infeasibility."
TOOLS[0]["description"] += " attach_md_source binds a registered, hash-verified historical candidate source chain to an empty idle study. It preserves original pose, loop-repair and cleanup receipts separately. It does not adopt historical jobs, approve compute, verify canonical target mapping, select an admitted MD pose or declare graph/parameterization completion. Reconsider archives an unexecuted historical source."

schema = deepcopy(UPSTREAM[0]["inputSchema"])
schema["properties"] = {k: v for k, v in schema["properties"].items()
                         if k not in {"evidence_id", "target", "site", "taxon", "scope"}}
schema["properties"].update(IDENTITY)
schema["properties"]["action"]["enum"] += ["resolve_target", "read_target", "search_parent_index", "export_parent_report", "export_design_report", "export_covdock_report"]
schema["properties"].update({
    "offset": {"type": "integer", "minimum": 0},
    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
})
schema["properties"].update({
    "design_id": {"type": "string", "pattern": "^design-[a-f0-9]{24}$"},
    "target_artifact_id": {"type": "string", "pattern": "^target-[a-f0-9]{32}$"},
    "target_manifest_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}})
schema["required"] = ["action", "workflow_id", "expected_revision"]
TOOLS.append({"name": EVIDENCE, "description": "Stage-scoped evidence work for the active Ligand-first workflow. "
    "Uses its bound target and latest evidence automatically; arbitrary evidence/design paths are not accepted. "
    "Before site selection, resolve_target retrieves verified UniProt identity and canonical cysteines (or reuses an explicit hash-bound target artifact); read_target revalidates it. A canonical cysteine is not a validated reactive site. "
    "search_parent_index queries the frozen CovPDB table for the verified target, with offset and limit (1–50). Preserve total_matches and source hashes. Returned residue/chain labels are unverified deposited identifiers, never canonical sites. Map and measure exact structures before selecting a pair. No hit permits supplementary PDB/literature discovery, not an infeasibility conclusion. "
    "collect/read retrieve the site-bound target evidence. supplement/search_parent_literature/measure_parent retain provenance "
    "and invalidate any prior active design handoff. read_sources reads original retrieved source records. "
    "finalize submits an evidence-supported parent comparison using the Hypothesis schema, only in PARENT_REVIEW. "
    "Pass the complete hypothesis object: route=ligand-first, rationale, claims, warhead_class, uncertainties, "
    "parent_ligand, parent_binding_mode=noncovalent, parent_evidence_ids, and nested parent_comparison. "
    "ParentComparison alone is not a Hypothesis. Claims and parent_evidence_ids cite retrieved source IDs; "
    "geometry IDs belong in parent_comparison.candidates[].geometry_id, not claim evidence_ids. "
    "NEEDS_INPUTS retains PARENT_REVIEW: complete the named fields and retry without removing comparison candidates. "
    "measure_parent requires molecule_id, pdb_id and component; supplement the exact ChEMBL record first. "
    "supplement requires source_ids, a list of 1-8 exact IDs such as ['PDB:1M17', 'ChEMBL:CHEMBL553']; "
    "do not pass pdb_id, molecule_id or query to supplement. "
    "read_sources accepts one to three source IDs per call. These action contracts are available here without reading local files. "
    "export_parent_report exports the current bound official proposal and report for host Results delivery; no paths or IDs are accepted. "
    "After report_covdock, use export_covdock_report to deliver its sealed report.json and report.md to Results. This requires a verified current LO report and never submits compute. "
    "export_design_report exports sealed design delivery/report.md and disposition.json; optional design_id must belong to this workflow or its archived branches. Omit it for the active design. "
    "Use it instead of artifact.save_file on remote storage paths. It performs no computation or scientific approval. "
    "At later stages call workflow reconsider to choose another parent. All existing scientific gates still apply.",
    "inputSchema": schema, "annotations": EXECUTION})
TOOLS.append({"name": DESIGN, "description": "Perform only the current design stage using inputs bound by the workflow. "
    "prepare uses its verified parent proposal; generate performs bounded local graph generation; plan_pose exports "
    "a bounded pose request without launching it; check_pose_runtime revalidates that export and compiles it "
    "through the configured registered executor without registration, approval or submission; "
    "deliver publishes machine-derived facts and limitations. "
    "start_pose submits only the frozen plan through native user approval; pose_status observes the bound job. "
    "evaluate_pose re-reads registered mapped products and recomputes existing geometry gates; it does not establish inhibition or MD readiness. "
    "prepare_refinement freezes only freshly revalidated geometry-passing candidates under the shared exact-input contract; it does not launch refinement. "
    "prepare_refinement_target binds the current study's sequence, deposited site mapping and receptor projection to those candidates; "
    "it saves AF3 seed/configuration drafts and a receptor preparation inventory. This does not prepare the receptor or launch computation. "
    "prepare_receptor_input packages the sealed study's original sources into the shared receptor-preparation input; "
    "check_receptor_runtime probes configured CPU dependencies and compiles the existing preparation engine. "
    "These actions neither repair a receptor nor submit a job; missing runtime is a configuration wait. "
    "start_receptor_preparation submits only its frozen CPU plan through separate native approval; "
    "receptor_preparation_status reads the same job without saving or advancing revision; refresh_receptor_preparation explicitly saves changed observations (no-op if unchanged), and reconcile restores a lost submission receipt without replay. "
    "evaluate_receptor replays registered prepared-output identity, stereochemistry and geometry, and measures descriptive site context; "
    "Before VS, receptor preparation binds the current VS study without requiring a refinement candidate. "
    "use_prepared_receptor_for_vs binds verified prepared coordinates to a new input, preserves old plans, "
    "and requires a fresh environment review and independent VS approval. Use only actions offered by the current stage. "
    "report_receptor freezes the revalidated review. Neither grants environment suitability, CovDock readiness or MD admission. "
    "prepare_covdock_input binds the reviewed receptor and exact candidates and returns source-bound environment review requirements; "
    "read_covdock_environment returns the current review, requirements and allowed evidence inventory. "
    "review_covdock_environment records an agent assessment addressing assembly, excluded components, construct/site and reaction scope, "
    "each with a decision, rationale, bound JSON file citations with RFC 6901 pointer locators/observations, and limitations. "
    "Pointers must resolve to existing fields; actual cited values are saved by the tool. "
    "Use evidence_insufficient when the sources cannot justify proceeding; do not infer suitability from SASA or removal alone. "
    "This is a provenance-checked judgment, not measured biological validity or user approval. Revised judgments retire the previous unexecuted plan. "
    "check_covdock_runtime compiles the existing CovDock engine when configured. These are planning-only actions: "
    "asset presence is not a successful license checkout, environment review or authorization to execute. "
    "start_covdock submits only the frozen plan after a source-bound positive environment assessment and separate native user approval; "
    "covdock_status observes the same signed task, and reconcile recovers a lost registration receipt without replay. "
    "Task completion does not imply accepted structures, inhibition or MD readiness. "
    "evaluate_covdock reopens registered structures with the frozen vendor reader and replays the approved provisional gates; "
    "report_covdock freezes all candidates, failures, missing evidence and limitations. Reader unavailability is not molecular failure. "
    "prepare_md_handoff revalidates current-study outputs and freezes a portable shared candidate delivery, preserving all failures. "
    "It reports pose-selection eligibility separately from native MD admission, which remains not evaluated; no compute or candidate selection is submitted. "
    "select_md_candidate binds one compound_id/input_sdf_index and an explicit scientific reason to that delivery. "
    "The selection is durable and cannot silently change; release_md_candidate archives the unexecuted decision with a reason. "
    "check_md_graph_runtime compiles the existing native covalent graph checker for that exact pose; missing runtime is a recoverable configuration wait. "
    "Planning and selection submit no computation and do not establish chemical validity, parameterization or MD readiness. "
    "start_md_graph requires separate native user approval for that frozen single-candidate CPU plan; md_graph_status observes its original signed task. "
    "Reconcile an uncertain submission without replay; do not release/reselect or replan a pending or registered graph task. "
    "Graph completion requires registered receipt, parent and artifact verification; it does not grant parameterization or MD readiness. "
    "plan_md_parameterization binds the verified registered graph and unchanged selected candidate to the shared native parameterization executor; runtime checks and planning submit no calculation. "
    "start_md_parameterization requires a separate native approval for that frozen plan; md_parameterization_status and reconcile observe the original task without duplicate submission. "
    "Parameters are accepted only after registered artifact hashes, completion receipt and original graph/candidate identity are revalidated. Parameterization completion is not MD-system readiness. "
    "plan_md_equilibration consumes the verified current short QC and freezes the existing 500 ps NPT protocol. "
    "start_md_equilibration requires independent exact-plan approval; md_equilibration_status verifies all registered frames and outputs. "
    "Protocol completion differs from stability gate acceptance; neither claims equilibrium convergence. Production and analysis remain unconnected. "
    "retry_md_system_preparation_plan, retry_md_short_qc_plan and retry_md_equilibration_plan only archive a freshly confirmed failed terminal task and freeze a new separately approved attempt; they never execute. "
    "System preparation and short QC each use plan_md_*, start_md_* with separate native approval, md_*_status, and release_md_*_plan for unsubmitted plans only. Inputs and bounded CPU protocols are workflow-owned. "
    "System preparation verifies conversion, forcefield equivalence and bounded minimization. Short QC verifies integration stability only; equilibration and production remain pending. "
    "release_md_parameterization_plan archives only an unsubmitted plan; pending or registered tasks cannot be replaced by replanning. "
    "Repeated status/report calls reuse only unchanged witnessed measurements; explicit evaluation reopens structures. "
    "check_af3_runtime compiles and freezes the current study's AF3 plan, with the data pipeline enabled; "
    "missing assets mean runtime configuration is required, not a GPU queue or molecular failure. "
    "start_af3 requires a separate native approval for that exact plan; af3_status observes the existing registered job. "
    "evaluate_af3 re-reads registered outputs and recomputes confidence and limited geometry for the full frozen candidate set; "
    "failed candidates remain explicit without inventing scores or molecular rejection. report_af3 saves a revalidated measurement report. "
    "Recovery observes the same job; neither action submits computation or grants scientific acceptance. "
    "AF3 completion is not independent result acceptance, CovDock receptor readiness or MD admission. "
    "report_pose revalidates the completed pose job and writes an exploratory candidate report, including zero-passing branches, "
    "the frozen numerical criteria, metric meanings, legacy labels and evidence gaps. It does not establish physical acceptance, inhibition or MD readiness. "
    "No free paths or substitute parent. Boolean approval is insufficient. A negative branch can return to parent review "
    "through workflow reconsider; evidence supplementation remains available.",
    "inputSchema": {"type": "object", "properties": {**IDENTITY,
        "action": {"type": "string", "enum": ["prepare", "generate", "plan_pose", "check_pose_runtime", "start_pose", "pose_status", "evaluate_pose", "prepare_refinement", "prepare_refinement_target", "prepare_receptor_input", "check_receptor_runtime", "start_receptor_preparation", "receptor_preparation_status", "refresh_receptor_preparation", "evaluate_receptor", "report_receptor", "use_prepared_receptor_for_vs", "prepare_covdock_input", "check_covdock_runtime", "start_covdock", "covdock_status", "evaluate_covdock", "report_covdock", "prepare_md_handoff", "select_md_candidate", "release_md_candidate", "check_md_graph_runtime", "start_md_graph", "md_graph_status", "plan_md_parameterization", "start_md_parameterization", "md_parameterization_status", "release_md_parameterization_plan", "plan_md_system_preparation", "start_md_system_preparation", "md_system_preparation_status", "release_md_system_preparation_plan", "retry_md_system_preparation_plan", "plan_md_short_qc", "start_md_short_qc", "md_short_qc_status", "release_md_short_qc_plan", "retry_md_short_qc_plan", "plan_md_equilibration", "start_md_equilibration", "md_equilibration_status", "release_md_equilibration_plan", "retry_md_equilibration_plan", "review_covdock_environment", "read_covdock_environment", "check_af3_runtime", "start_af3", "af3_status", "evaluate_af3", "report_af3", "report_pose", "deliver"]},
        "environment_review": REVIEW_SCHEMA,
        "compound_id": {"type": "string", "minLength": 1},
        "input_sdf_index": {"type": "integer", "minimum": 0, "maximum": 9007199254740991},
        "reason": {"type": "string", "minLength": 1, "maxLength": 8000},
        "human_approved": {"type": "boolean", "default": False}, "approval_note": {"type": "string"}},
        "required": ["action", "workflow_id", "expected_revision"], "additionalProperties": False},
    "annotations": EXECUTION})


for tool in TOOLS:
    tool["outputSchema"] = output_schema(INTERFACE["route"], INTERFACE["protocol"],
                                         INTERFACE["id_pattern"], tuple(INTERFACE["tools"]))
TOOLS[0]["inputSchema"]["x-research-workflow"] = deepcopy(INTERFACE)

# Historical input retains a separate source record; execution and approval
# remain the same registered graph and parameterization actions above.
design_schema = next(tool for tool in TOOLS if tool['name'] == DESIGN)
from ..receptor_environment_review import EVIDENCE_READ_SCHEMA, READ_GUIDANCE
design_schema['inputSchema']['properties'].update(deepcopy(EVIDENCE_READ_SCHEMA))
design_schema['description'] += ' read_vs_input and read_covdock_environment expose bound JSON and PDB headers. ' + READ_GUIDANCE
design_schema['inputSchema']['properties']['candidate_ids'] = {
    'type': 'array', 'minItems': 1, 'maxItems': 3, 'uniqueItems': True,
    'items': {'type': 'string', 'minLength': 1}}
design_schema['inputSchema']['properties']['action']['enum'] += [
    'prepare_docking_input', 'prepare_direct_lo', 'prepare_direct_lo_target',
    'prepare_vs_refinement', 'prepare_vs_refinement_target',
    'prepare_vs_input', 'read_construct_evidence', 'review_construct', 'select_receptor_conformers', 'read_vs_input', 'review_vs_environment', 'check_vs_runtime', 'start_vs', 'vs_status', 'evaluate_vs', 'report_vs']
design_schema['inputSchema']['properties']['docking_route'] = {
    'type': 'string', 'enum': ['auto', 'direct_lo', 'vs_then_lo'], 'default': 'auto'}
design_schema['inputSchema']['properties']['construct_review'] = {
    'type': 'object', 'additionalProperties': False,
    'required': ['evidence_sha256', 'decision', 'rationale', 'limitations', 'segments'],
    'properties': {
        'evidence_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
        'decision': {'type': 'string', 'enum': ['retain_deposited_construct', 'requires_revision', 'evidence_insufficient']},
        'rationale': {'type': 'string', 'minLength': 20, 'maxLength': 4000},
        'limitations': {'type': 'array', 'minItems': 1, 'maxItems': 16,
                       'items': {'type': 'string', 'minLength': 5, 'maxLength': 1000}},
        'segments': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'required': ['locator', 'observation', 'rationale'], 'properties': {
                'locator': {'type': 'string'},
                'observation': {'type': 'string', 'minLength': 20, 'maxLength': 2000},
                'rationale': {'type': 'string', 'minLength': 20, 'maxLength': 2000}}}}}}
design_schema['inputSchema']['properties']['environment_review'] = deepcopy(REVIEW_SCHEMA)
for item in design_schema['inputSchema']['properties']['environment_review']['properties'].values():
    item['properties']['decision']['enum'] = [*item['properties']['decision']['enum'], 'suitable_for_exploratory_vs']
design_schema['description'] += (' read_construct_evidence is a read-only diagnostic for the source-bound sequence audit, including '
    'unaligned segments and current blockers; it neither approves the construct nor creates VS input. '
    'review_construct records a judgment against that exact evidence hash and every segment locator. '
    'The retain_deposited_construct scope keeps the full sequence and coordinates; it cannot admit '
    'internal gaps, nonstandard residues, sequence differences or unverified species. Never infer '
    'that a terminal segment is a tag or harmless; use evidence_insufficient when unsupported. '
    'This does not prepare a receptor, grant resource approval or establish scientific suitability. '
    'prepare_vs_input binds candidates directly from the current sealed design '
    'to an audited receptor projection for CovDock-VS, preserving historical pose jobs. '
    'It reports excluded components, missing residues and original-to-projected site mapping. '
    'read_vs_input revalidates that source binding and returns the allowed citation inventory. '
    'To read actual evidence, call read_vs_input with evidence_path from that inventory. '
    'Use evidence_locator (RFC 6901 pointer, empty for root) and evidence_offset for returned pages; '
    'complete=true identifies an exact value, while children expose real pointers for further reading. '
    'Inspect the required assembly, excluded-component, construct/site and reaction evidence this way '
    'before reviewing; do not guess pointers or use local file tools on remote paths. '
    'review_vs_environment records a source-bound assessment of assembly, excluded components, construct/site '
    'and reaction scope; use suitable_for_exploratory_vs only for a justified VS assessment, otherwise '
    'requires_revision or evidence_insufficient. A changed review retires the previous unsubmitted plan. '
    'check_vs_runtime compiles a private, bounded CPU plan '
    'without submitting a job. These actions do not repair missing residues, approve the environment, '
    'check out a vendor license or grant MD admission. start_vs requires exact native user approval '
    'and registers one immutable task; vs_status and reconcile observe that same task without replay. '
    'Task registration is not scheduler submission and task completion is not scientific acceptance. '
    'evaluate_vs independently re-reads registered raw Maestro output, verifies candidate/atom lineage and the Michael product, '
    'then measures scaffold displacement, contacts, local relaxation energy, product geometry and clashes with each pose own receptor. '
    'report_vs saves the full frozen review and Markdown report, including missing and failed candidates; save these to Results. '
    'Passing provisional geometry permits further review only; native MD admission remains a separate gate. '
    'prepare_vs_refinement freezes explicit candidate_ids from jointly passing VS candidates with a reason, '
    'preserving original input order and the signed VS evidence. prepare_vs_refinement_target then binds the target; '
    'prepare_receptor_input/check_receptor_runtime lead to separately approved receptor preparation and CovDock-LO. '
    'LO re-docks the selected pre-reaction molecules; no VS result is an MD input and no approval transfers. '
    'An existing frozen subset cannot be replaced in place; reconsider the study for a new branch.')
design_schema['inputSchema']['properties']['action']['enum'].append('prepare_historical_md_candidate')
design_schema['inputSchema']['properties'].update({
    'target_artifact_id': {'type': 'string', 'pattern': '^target-[a-f0-9]{32}$'},
    'target_manifest_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}})
design_schema['description'] += (' prepare_historical_md_candidate binds the attached historical candidate to a verified target identity and exact canonical construct sequence. '
    'It freezes a distinct historical pose input and selects that candidate with a reason, without adopting old execution results. '
    'check_md_graph_runtime then prepares the existing native graph engine; start_md_graph requires its own native approval. '
    'Native historical inspection checks source pose identity, retained heavy graph, cleanup, actual protein sequence/numbering, peptide connectivity and new-loop clashes before common covalent graph admission. '
    'Sequence verification alone does not establish native structure mapping, reactivity or MD readiness.')


def handlers(root: Path, arguments: dict):
    return {ENTRY: lambda: navigate(root, **arguments), EVIDENCE: lambda: step(root, EVIDENCE, **arguments),
            DESIGN: lambda: step(root, DESIGN, **arguments)}

design_schema['inputSchema']['properties']['action']['enum'].append('select_restrained_receptor_policy')
design_schema['description'] += (' select_restrained_receptor_policy requires a reason and a verified failed pre-VS or direct-LO receptor task; it preserves the old task and selects position-restrained observed-atom motion for a new separately approved plan. It does not submit computation or grant receptor admission.')

from ..receptor_environment_review import MOTION_REVIEW_SCHEMA
design_schema['inputSchema']['properties']['motion_review'] = deepcopy(MOTION_REVIEW_SCHEMA)
design_schema['inputSchema']['properties']['action']['enum'] += ['read_receptor_motion', 'review_receptor_motion']
design_schema['description'] += (' read_receptor_motion reads the current independently replayed v2 receptor measurements. review_receptor_motion records all four source-bound judgments with current evidence citations; it does not itself grant receptor admission or authorize calculation.')

# Local observed-coordinate repair is a separately frozen selection, never an automatic retry.
design_schema['inputSchema']['properties']['action']['enum'].append('select_local_receptor_policy')
design_schema['description'] += (' select_local_receptor_policy derives a bounded whole-residue observed-atom mask from the current registered v1 source clashes, protects the reactive Cys, and binds differentiated restraints and displacement limits. It preserves the failed task, does not execute, and requires a newly compiled exact native-approved plan. It is distinct from global v2 observed-atom relaxation.')

design_schema['inputSchema']['properties']['action']['enum'].append('select_bounded_local_receptor_policy')
design_schema['description'] += (' select_bounded_local_receptor_policy preserves a verified failed harmonic-local task and its original source mask, then selects a distinct hard-displacement-ball optimizer proposal. No execution or retry occurs; a new exact native-approved plan is required.')

design_schema['inputSchema']['properties']['action']['enum'].append('select_block_local_receptor_policy')
design_schema['description'] += (' select_block_local_receptor_policy preserves a signed bounded iteration-limit failure and its original source mask, then selects a distinct free-atom warmup and joint bounded proposal. Selection does not execute; a new exact native-approved plan is required.')


design_schema['inputSchema']['properties']['conformer_selection'] = {
    'type': 'object', 'additionalProperties': False,
    'required': ['evidence_sha256', 'choices', 'rationale', 'limitations'],
    'properties': {
        'evidence_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
        'choices': {'type': 'object', 'additionalProperties': {'type': 'string'}},
        'rationale': {'type': 'string', 'minLength': 1},
        'limitations': {
            'oneOf': [
                {'type': 'string', 'minLength': 1},
                {'type': 'array', 'minItems': 1,
                 'items': {'type': 'string', 'minLength': 1}},
            ]}}}
design_schema['description'] += (
    ' read_construct_evidence also returns receptor_conformers with its own evidence_sha256. '
    'select_receptor_conformers requires conformer_selection against that hash, with an explicit '
    'choice for every listed author residue and none others, rationale and nonempty limitations '
    '(either one string or a list of strings; lists are normalized into the stored v1 string). '
    'Do not infer that A or greater occupancy is scientifically optimal. '
    'Selection grants no computation approval and becomes immutable in downstream inputs.')

# Direct LO consumes original designed molecules, never a fabricated VS review.
design_schema['description'] += (
    ' For Ligand-first batches of up to six chemically verified, stereochemically explicit candidates, '
    'default to prepare_docking_input (docking_route=auto), which counts the full admitted batch and freezes '
    'direct LO for at most six, or VS preselection for a larger batch. An explicit docking_route requires reason. '
    'Follow prepare_direct_lo_target and independent receptor preparation on the direct route. '
    'These actions do not run VS, claim pose acceptance, or grant computation approval. '
    'The frozen input records the full generated batch and explicit deferrals. '
    'Larger batches use VS preselection followed by independently approved LO; VS is optional for small batches.')
