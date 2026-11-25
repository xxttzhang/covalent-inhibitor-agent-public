---
name: library-first-benchmark
description: Run, validate, freeze, and audit the BTK-mini covalent-inhibitor Library-first benchmark. Use when checking the five supported scoring routes, comparing enrichment metrics, freezing authoritative artifacts, verifying checksums, or refusing conclusions from incomplete or fabricated result tables.
---

# Library-first Benchmark

Treat the checked-in benchmark specification and frozen manifest as the source of truth. Preserve failed compounds as explicit failures and never invent replacement scores.

## Workflow

1. Read `references/protocol.md` before operating on BTK-mini.
2. Confirm that the library has 420 unique compounds: 20 actives and 400 decoys.
3. Validate all five result tables with `scripts/validate_btk_workflow.py`.
4. Freeze artifacts only with `scripts/freeze_benchmark.py freeze`.
5. Before comparing methods or reporting metrics, run `scripts/freeze_benchmark.py verify`.
6. Report the denominator, success/failure counts, score direction, failure policy, and manifest identifier with every comparison.

## Guardrails

- Do not rerun docking merely to make a validation failure disappear.
- Do not delete failed rows or calculate metrics only on successful rows unless clearly labelled as a sensitivity analysis.
- Do not overwrite an existing frozen manifest.
- Do not compare raw scores across engines as if their numerical scales were calibrated.
- Stop and request human review when an input or result checksum differs from the frozen manifest.

## Commands

Use paths relative to the project root:

```bash
python scripts/freeze_benchmark.py verify \
  --manifest benchmark/frozen/BTK-mini-v1/manifest.json
```

```bash
python scripts/science_state.py audit \
  --db state/science_state.sqlite3 \
  --project-root .
```
