"""Route-neutral covalent-site discovery MCP contracts."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .annotations import EXECUTION, IDEMPOTENT_WRITE
from covscreen.covalent_site_discovery import (
    evaluate_covalent_sites_tool,
    predict_cysteine_sites_tool,
    select_covalent_sites_tool,
)


from covscreen.site_contracts import (
    EvaluationInput, SelectionInput, input_schema, prediction_schema,
)


TOOLS = [
    {
        "name": "covalent_predict_cysteine_sites",
        "description": (
            "Compile, start or inspect a route-neutral DeepCoSI prediction over all Cys residues in one "
            "hash-bound receptor-input structure. It preserves raw scores, per-site failures, pockets, "
            "canonical/author numbering and source/model hashes. Use DeepCoSI when literature evidence is absent or insufficient; literature is not a prerequisite for prediction. Scores are uncalibrated ranking values, not experimental validation. "
            "This registered implementation is CPU-only (gpu_count must be 0), so missing CUDA is not a "
            "blocker. For plan/start, receptor_input_artifact_id AND manifest_sha256 from the same acquisition are required. For status, only action and the submitted run_id are allowed. Omit resource_budget to retain defaults. Starting prediction requires native user approval and never starts screening."
        ),
        "inputSchema": prediction_schema(),
        "annotations": EXECUTION,
    },
    {
        "name": "covalent_evaluate_covalent_sites",
        "description": (
            "Read a completed DeepCoSI artifact, revalidate its exact manifest, join optional sealed "
            "target-hypothesis evidence by canonical Cys and rank structurally valid candidates without "
            "turning missing evidence into negative evidence. hypothesis_evidence_ids may be omitted when no literature or other sealed evidence is available; retain evidence_status=unknown and still evaluate structural/model candidates. Produces an immutable evaluation artifact."
        ),
        "inputSchema": input_schema(EvaluationInput),
        "annotations": IDEMPOTENT_WRITE,
    },
    {
        "name": "covalent_select_covalent_sites",
        "description": (
            "Freeze one or more eligible candidate Cys sites from an immutable evaluation. Selection may "
            "use explicit canonical positions or a bounded top-ranked exploratory policy. Emits one shared, "
            "hash-bound route-input artifact for Library-first, Ligand-first and/or De novo; it does not "
            "register a campaign or claim site validation."
        ),
        "inputSchema": input_schema(SelectionInput),
        "annotations": IDEMPOTENT_WRITE,
    },
]


def handlers(root: Path, arguments: dict[str, Any]) -> dict[str, Callable[[], dict[str, Any]]]:
    return {
        "covalent_predict_cysteine_sites": lambda: predict_cysteine_sites_tool(root, arguments),
        "covalent_evaluate_covalent_sites": lambda: evaluate_covalent_sites_tool(root, arguments),
        "covalent_select_covalent_sites": lambda: select_covalent_sites_tool(root, arguments),
    }
