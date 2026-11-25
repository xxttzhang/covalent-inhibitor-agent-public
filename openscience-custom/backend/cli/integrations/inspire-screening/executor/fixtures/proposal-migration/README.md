# Pre-migration proposal fixtures

These two SQLite databases and their JSON proposal records were generated with the existing
`setup` software fixture and the unmodified v3/v4 proposal producer, **before** the v5/v6 edit.
The capture test passed before migration. Producer and fixture hashes are in provenance.json;
the producer backup and capture log are retained in
`reports/next-stage-plan-20261022/proposal-migration/` at the workspace root.

The tests copy each database into an isolated temporary directory, reopen it through the
actual Store, and check the original proposal and digest. They do not reconstruct a legacy
proposal with the new producer. Do not regenerate these fixtures to make a migration pass.

All molecular, resource, account and approval records are synthetic software fixtures.
No remote commands, GPU calculations or real user approvals are represented.
