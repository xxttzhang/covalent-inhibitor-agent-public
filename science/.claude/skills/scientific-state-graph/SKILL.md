---
name: scientific-state-graph
description: Curate and audit structured scientific state for covalent-inhibitor discovery. Use when registering targets, residues, compounds, hypotheses, evidence, artifacts, benchmark runs, provenance, confidence, or contradictory findings in the SQLite evidence graph.
---

# Scientific State Graph

Use the evidence graph as an auditable record of what is known, inferred, measured, and still uncertain. Keep observations separate from interpretations.

## Workflow

1. Read `references/schema.md` before adding or interpreting graph records.
2. Initialize the database with `scripts/science_state.py init`.
3. Register a research dossier with `scripts/science_state.py ingest-project`.
4. Register BTK-mini artifacts with `scripts/science_state.py ingest-btk` only when
   auditing that regression benchmark.
5. Inspect counts and key records with `scripts/science_state.py summary`.
6. Verify artifact existence and checksums with `scripts/science_state.py audit`.
7. Export a portable snapshot with `scripts/science_state.py export`.

## Evidence Rules

- Every factual claim must point to an evidence record or a hashed artifact.
- Record confidence as a number in `[0, 1]`; confidence is not probability unless explicitly modelled as such.
- Mark support, contradiction, and uncertainty explicitly.
- Preserve negative results and engine failures.
- Use stable identifiers. Do not silently rename an entity after it has been cited.
- Never modify benchmark artifacts through the graph utility.
- Do not promote a hypothesis to accepted based only on an agent narrative.

## Static-Phase Boundary

This skill may update the structured state only when a human explicitly authorizes the write. It must not edit source code, prompts, skills, scoring functions, benchmark specifications, or frozen manifests.
