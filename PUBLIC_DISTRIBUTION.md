# Public distribution boundary

This repository is a source-visible public candidate made from committed private source
revision `736b9fcec63e67e89221f7269fdb0691c70a1a49`. It is deliberately separate from the
private development repository and starts with a fresh Git history.

## Included

- Application, workflow and scientific orchestration source code.
- Tests that do not require excluded project scientific data.
- Configuration examples without credentials.
- OpenScience, InterMol and PDBFixer code with their existing notices.

## Excluded

- Uncommitted private development changes and the private Git history.
- Credentials, provider configuration, approval keys and license files.
- Sessions, task ledgers, logs, reports, screenshots, research outputs and model weights.
- `drug-idea-generator`, because no redistribution license was supplied.
- Project scientific fixtures, structures, candidate indices and De novo target assets
  whose public redistribution provenance has not been completed.

Excluded scientific inputs and engines must be acquired independently under suitable
terms and supplied through documented external paths. Their absence is reported as a
configuration/input error; the public repository does not silently substitute results.

## License status

Original project contributions are currently source-visible with no reuse license.
Third-party subtrees retain their own terms. If an OSI-approved license is selected for
the original work later, replace the root notice only after confirming contributor
ownership and completing the remaining bundled-asset review.
