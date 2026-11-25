---
name: covalent-job-execution
description: Plan, submit, monitor, cancel, and retry registered covalent-discovery jobs through the OpenScience MCP execution boundary. Use for platform health checks, RDKit warhead enumeration, CarsiDock-Cov docking, RTMScore rescoring, PocketXMol, REINVENT4, DiffLinker, job status or logs, candidate-table inspection, and approved Library-first, Ligand-first, or De novo computation on Inspire.
---

# Covalent Job Execution

Use only registered engine adapters. Preserve every input identity, failure, log, parameter, resource budget, and output checksum in the job registry.

## Workflow

1. Call `covalent_platform_health` and `covalent_runtime_preflight`. Stop only
   when the job registry, input chain, or assets required by the exact next tool
   fail. Match its declared `resource_budget` to preflight `resource_classes`;
   missing CUDA blocks GPU plans but does not block CPU-only tools such as shared
   DeepCoSI site prediction.
2. Validate the project and route. For BTK-mini, also use `library-first-benchmark` and verify the frozen benchmark before interpreting metrics.
3. Build a `covalent_submit_job` request with a registered engine, project-local inputs, explicit `ResourceBudget`, and `execution_mode: plan`.
4. Present the compiled command, inputs, outputs, candidate limit, runtime limit, GPU count, and stop conditions.
5. Switch to `execution_mode: execute` only when the user has explicitly approved that exact computation. Set `human_approved: true` and record a meaningful `approval_note`.
6. Poll `covalent_get_job` until a terminal state. Use `covalent_get_job_log` for diagnostics and `covalent_get_candidates` for the identity-mapped result table.
7. Use `covalent_retry_job` only for `FAILED`, `TIMED_OUT`, or `CANCELLED` runs and only with a new explicit approval. Treat the retry as a new run linked to its parent.
8. Report artifact paths, checksums, success/failure counts, failure reasons, elapsed time, and resource budget. Never promote a computational result to experimental evidence.

## Registered engines

Use these execution adapters:

- `rdkit-warhead-enumeration`: deterministic enumeration for all three routes
- `carsidock-cov`: identity-mapped covalent docking with explicit failed rows
- `rtmscore`: pose-only rescoring of successful CarsiDock poses while retaining upstream failures
- `library-first-evaluation`: conservative enrichment metrics, ranked candidates, and a traceable report
- `pocketxmol`: pocket-conditioned small-molecule SBDD proposals for the De novo route
- `reinvent4`: PubChem-prior de novo SMILES proposals; NLL is model likelihood, not affinity
- `difflinker`: pocket-conditioned fragment linking for Ligand-first or De novo

Do not substitute arbitrary shell commands. With the currently configured assets, PocketXMol and
REINVENT4 are De novo-only; DiffLinker is registered for Ligand-first and De novo. Every generated
molecule remains a proposal until it passes chemistry, covalent geometry, reactivity, ADMET, and
experimental gates.

## Safety rules

- Default to plan mode; approval never carries over to a different engine, input set, budget, or retry.
- Reject arbitrary commands, paths outside the project, missing input identities, and unbounded resources.
- Cancel only when the user explicitly approves cancellation.
- Keep partial outputs and failures. Do not repair a denominator by deleting failed rows.
- Stop on checksum mismatch in a frozen benchmark.
- Keep API keys, tokens, and commercial license values out of requests, logs, and reports.
- Distinguish prediction, computational evaluation, and experimental validation.

For Library-first, require `ligand_table` and `ligand_sdf` identity alignment before CarsiDock. Pass the emitted `carsidock_scores.parquet` to RTMScore; never rerun docking merely to rescore an existing pose.
