"""Interpret existing producer fields without changing their frozen gate policies.

These contracts describe the implementation, not experimentally validated cutoffs.
Producer names are scoped: identically named metrics are not interchangeable.
"""
from copy import deepcopy


def md_not_evaluated():
    """Explicit downstream non-assessment, not a negative admission decision."""
    return {'state': 'not_evaluated', 'scope': 'native_md_admission',
            'reason': 'No native MD admission assessment was performed by this operation.',
            'rejection_reasons': [], 'md_ready': False}


def result_state_semantics():
    """Interpret observed status fields without adding measurements or decisions."""
    return {
        "execution_return_code": "A batch/process exit code belongs to that batch/process. Do not assign it to a candidate without a candidate-specific exit record.",
        "engine_status": "Candidate SUCCESS records an accepted engine/parser output under that producer. It does not by itself prove numerical convergence, physical validity or activity. FAILED does not establish molecular inactivity.",
        "physical_validity": "PASS covers only the named measured checks under the frozen policy, not complete topology, stereochemistry, binding or reaction validation. UNKNOWN means insufficient measurements, not a measured failure.",
        "covdock_gate": "Preserve the recorded legacy gate and detail alongside independent axes. C can include execution/score failure even when physical_validity is UNKNOWN; do not relabel C as a measured physical failure or erase it as not applicable.",
        "validated_inhibitor_false": "Not established by these results. This is not a negative activity measurement or evidence that the candidate is not an inhibitor; report unknown/not validated, not an inhibitor-status cross.",
        "review_eligible": "Meets the explicitly identified candidate-selection policy, not permission to read or review evidence. Translate false as 未满足本次入选条件, never 不可复核. Existing records can still be inspected. Legacy strict CovDock v3 requires evaluated HIGH or LOW consensus; Ligand v4 requires successful execution and complete physical PASS or WARN, with AF3 optional and absent comparison remaining NA. A v4 reassessment preserves original frozen gates separately. Filling one missing gate does not establish inhibition or MD readiness.",
        "decision_scope": "Candidate-selection policy, MD admission and experimental activity are separate decisions. Do not present gate A -> MD -> validated inhibitor as a universal prerequisite chain. Cross-method agreement and MD are not universal prerequisites for experimental activity evidence; report what this frozen policy actually decides.",
        "md_ready_false": "Read the explicit md_admission state. not_evaluated means no MD admission assessment was performed; it is not a rejection and has no inferred rejection reasons. Candidate selection or report completion does not perform that assessment.",
        "released_selection": "release_md_candidate deselects and archives a choice; selection_released means 已取消当前选择并归档, never 已发布. Read the current md_candidate_selection state; archived decisions and their plans are historical, not active or executable.",
        "operation_submission": "calculation_submitted=false and compute_submitted_by_report=false describe that operation, not the entire candidate history. A missing submission alone is not an error or scientific rejection; distinguish recorded prerequisite failures from ordinary future work.",
        "resource_scope": "Use the exact plan resource budget. gpu_count=0 means no GPU is required for that plan; gpu_submitted=false is not a GPU shortage, queue or blocker for it. Budgets are limits, not measured allocation or consumption.",
        "executor_identity": "Use the plan's explicit engine field for its executor. discovery_origin.task_id identifies an upstream source, not necessarily the current executor. Missing names or abbreviation definitions remain unknown; do not guess their expansions from identifiers.",
        "missing_measurements": "A missing/null score is 未测得/无评分, never a numerical zero. Zero measured candidates is a count, not a zero-valued score. Preserve the original null and its reason.",
        "producer_scope": "Producer/module is not synonymous with route. Ligand-first pose geometry and CovDock refinement can be stages of the same Ligand-first workflow; CovDock is also shared by other routes. Compare their definitions without inventing a route switch.",
        "artificial_evidence": "Respect explicit fixture/synthetic provenance. Placeholder filenames and successful fixture statuses do not establish real coordinates, scientific engine execution or measurements; do not suggest reopening a placeholder as a real structure.",
    }


def _metric(field, unit, definition, supports, limitation):
    return {"field": field, "unit": unit, "definition": definition,
            "supports": supports, "does_not_establish": limitation}


_GROUPS = [
    {"producer": "covindb_descriptor_filter", "route": "library-first", "stage": "chemical",
     "implementation": "routes/library-first/src/covscreen_library_first/covindb.py",
     "policy_source": "routes/library-first/covindb-configs/EGFR_C797_mvp_v1.yaml",
     "applicability": "Descriptor methods apply to the parsed molecule; this named threshold profile is EGFR C797/acrylamide specific",
     "metrics": [
         _metric("mw", "Da", "RDKit Descriptors.MolWt on the parsed molecule", "Molecular size filter", "Affinity or permeability"),
         _metric("clogp", "dimensionless", "RDKit Descriptors.MolLogP on the parsed molecule", "Calculated lipophilicity filter", "Measured logD, solubility or exposure"),
         _metric("tpsa", "angstrom^2", "RDKit rdMolDescriptors.CalcTPSA", "Topological polar surface filter", "Measured permeability"),
         _metric("rotatable_bonds", "count", "RDKit Lipinski.NumRotatableBonds", "Rotatable-bond count filter", "Binding entropy"),
         _metric("formal_charge", "elementary_charge", "RDKit Chem.GetFormalCharge; gate uses absolute value", "Formal charge filter for the supplied molecular representation", "Physiological protonation distribution"),
         _metric("warhead_count", "count", "Number of configured warhead SMARTS matches; electrophile_atom_index is separately resolved or -1 when ambiguous", "Presence of the configured substructure", "Target reaction, reaction rate or selectivity"),
     ]},
    {"producer": "ligand_first_pose_geometry", "route": "ligand-first", "stage": "adduct",
     "implementation": "routes/ligand-first/src/covscreen_ligand_first/ligand_first_geometry.py",
     "policy_source": "LigandFirstGeometryResult.policy / LigandFirstGeometryPolicy",
     "applicability": "Mapped parent and CarsiDock candidate, explicit receptor target SG and mapped beta/alpha atoms; defaults are not universally calibrated",
     "evaluation_versions": {
         "legacy_unversioned": "Preserve original PASS/FAIL labels; missing strain or empty contacts may appear as failed gates.",
         "ligand-geometry-qc-v2-provisional": "Missing gates are null/UNKNOWN. Physical checks, design constraints and technical QC are separate; review eligibility is not activity or MD readiness. The frozen policy retains provisional numeric cutoffs.",
     },
     "metrics": [
         _metric("scaffold_rmsd_angstrom", "angstrom", "Mapped protected parent scaffold displacement in the producer coordinate frame", "Parent scaffold pose retention", "Affinity retention"),
         _metric("interaction_retention_fraction", "fraction", "Retained protected interactions divided by configured protected interactions; legacy empty set reports 0; v2 reports null", "Retention of the specified interaction set", "All binding interactions or affinity; empty set is not a measured loss"),
         _metric("covalent_cs_bond_distance_angstrom", "angstrom", "Distance from docking dummy sulfur to mapped beta carbon", "Internal docking geometry", "Actual receptor SG placement or chemical bond formation"),
         _metric("actual_sg_cbeta_distance_angstrom", "angstrom", "Distance from actual target receptor SG to mapped beta carbon", "Product-pose target proximity", "Reaction feasibility or a measured covalent bond"),
         _metric("attack_angle_s_cbeta_calpha_degrees", "degree", "Dummy S–beta–alpha angle with beta as vertex", "Internal docking orientation", "Prereaction attack trajectory or activation barrier"),
         _metric("actual_sg_cbeta_calpha_angle_degrees", "degree", "Actual receptor SG–beta–alpha angle with beta as vertex", "Product-pose target orientation", "Prereaction attack trajectory or reaction kinetics"),
         _metric("dummy_to_target_sg_distance_angstrom", "angstrom", "Docking dummy sulfur to actual receptor SG distance", "Alignment of dummy anchor with receptor target", "Binding strength"),
         _metric("strain_excess_over_parent_kcal_mol", "kcal/mol", "Candidate local relaxation strain minus parent strain only when force fields agree and values exist", "Relative local deformation under the recorded estimator", "Reaction barrier, binding free energy or global conformational minimum"),
         _metric("clash_fraction", "fraction", "Clashing ligand heavy atoms divided by ligand heavy atoms using recorded clash_method and exclusions", "Steric consistency under the recorded radii/exclusion policy", "Energetic feasibility or selectivity"),
     ]},
    {"producer": "covdock_gate", "route": "shared", "stage": "adduct",
     "implementation": "science/src/covscreen/covdock_gate.py",
     "policy_source": "assess_covdock_candidates arguments and saved report parameters",
     "applicability": "Bound CovDock output and its corresponding cross-method pose comparison; preserve original thresholds and scoring method",
     "evaluation_versions": {
         "covdock-lo-pose-gate-v2.1": "Legacy local_strain_energy uses the first finite supported vendor strain column; retain its original source and limitations.",
         "covdock-lo-pose-gate-v3.0-provisional": "Only r_strain_Local_Strain_Energy supplies local strain. Other vendor columns do not fill its absence; absent/nonfinite values remain UNKNOWN. This does not change frozen numerical cutoffs.",
     },
     "metrics": [
         _metric("local_strain_energy", "kcal/mol", "Vendor-reported local relaxation strain with version-specific source selection: v2.1 finite-column fallback; v3 only r_strain_Local_Strain_Energy. The estimator/reference is vendor-specific, not an absolute molecular energy or a candidate-minus-parent difference", "Vendor-reported local strain warning/rejection under the producing policy", "Comparable energy across force fields, candidate-parent strain increment, reaction barrier or binding free energy"),
         _metric("covalent_bond_length_angstrom", "angstrom", "Reported product covalent bond length", "Product bond geometry check", "Spontaneous covalent bond formation"),
         _metric("af3_covdock_ligand_rmsd_angstrom", "angstrom", "Reported AF3 versus CovDock ligand pose RMSD", "Cross-method pose agreement annotation", "Either method is correct, affinity or independent experimental validation"),
     ]},
]


def metric_contracts():
    """Return isolated read-only discovery data; never evaluate or rewrite results."""
    return {"schema": "producer-metric-contracts-v1", "thresholds_validated": False,
            "result_state_semantics": result_state_semantics(),
            "missing_policy": "Missing/nonfinite evidence is unknown, never an invented zero or pass. Preserve legacy outputs and expose their documented limitations.",
            "policy_rule": "Use the producing run's frozen policy. Do not substitute a current default or transfer thresholds across stage, target, warhead or method.",
            "groups": deepcopy(_GROUPS)}


def workflow_metric_context(route, phase):
    """Stage-scoped explanations, never gate decisions or replacement policies."""
    contracts = metric_contracts()
    producers = set()
    if route == "library-first" and (phase.startswith("SCREENING_") or phase == "CANDIDATE_REVIEW"):
        producers.add("covindb_descriptor_filter")
    if route == "ligand-first" and (phase.startswith(("DESIGN_", "POSE_")) or phase == "NEEDS_POSE_EVALUATION"):
        producers.add("ligand_first_pose_geometry")
    if phase.startswith(("REFINEMENT_", "COVDOCK_EXECUTION_")):
        producers.add("covdock_gate")
    return {"schema": contracts["schema"], "route": route, "workflow_phase": phase,
            "role": "interpretation_only_not_a_gate_result", "thresholds_validated": False,
            "missing_policy": contracts["missing_policy"], "policy_rule": contracts["policy_rule"],
            "result_state_semantics": contracts["result_state_semantics"],
            "groups": [group for group in contracts["groups"] if group["producer"] in producers]}
