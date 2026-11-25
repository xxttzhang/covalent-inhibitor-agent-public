# Scientific state graph schema

## Core records

- `entities`: durable scientific objects such as targets, residues, compounds, benchmarks, and engines.
- `evidence`: publications, measurements, result tables, and other traceable support.
- `relations`: typed edges between entities; each edge may cite evidence and confidence.
- `hypotheses`: testable statements with lifecycle status, assumptions, and disconfirming evidence.
- `hypothesis_evidence`: explicit support, contradiction, or context links.
- `artifacts`: files with SHA-256, producer, type, and metadata.
- `benchmark_runs`: engine-level executions tied to a frozen benchmark manifest.

## Provenance requirements

File-based evidence must use a project-relative path and SHA-256. External evidence must use a resolvable citation or URI and, where possible, a precise locator. JSON metadata must contain only supplementary attributes, not identifiers that belong in first-class columns.

## Hypothesis lifecycle

Use `proposed`, `testing`, `supported`, `contradicted`, or `retired`. Status changes require a human decision and new evidence links. Store observations independently of the hypothesis so contradictory evidence remains visible.

## Confidence

Use values from 0 to 1. Record the basis in metadata. Confidence expresses the curator's current support for a relation; it must not be interpreted as a calibrated probability without an explicit statistical model.
