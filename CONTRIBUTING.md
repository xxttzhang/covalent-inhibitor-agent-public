# Contributing

This is a public source snapshot, not an open-source project. Licensing for original contributions is not yet selected; obtain written maintainer agreement before submitting external contributions. Opening an issue or pull request does not grant a license to existing code or automatically establish terms for the contribution.

Keep source, runtime state, research outputs and credentials separate. Do not weaken scientific gates to obtain a successful run. Preserve failed-task evidence and use new approval for a new computation. Follow applicable AGENTS.md instructions.

## Checks

- `python3 ops/publication/check_source.py`: bounded distribution check.
- In `science/`, using the configured development environment: `python -m pytest`.
- In `openscience-custom/backend/cli/`: `bun test` and `bun run typecheck`.
- In `openscience-custom/frontend/workspace/`: `bun run typecheck` and `bun run build`.

Choose targeted tests for the affected contracts before a broader suite. Record external runtime/fixture requirements and any skipped tests. Source checks do not imply scientific acceptance. Never submit HPC jobs as part of an unapproved CI run.
