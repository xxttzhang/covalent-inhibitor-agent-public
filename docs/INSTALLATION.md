# Installation and external dependencies

This is a developer preview; a clean-machine end-to-end installation has not yet been validated.

## Python packages

From the repository root, use Python 3.11 and a dedicated virtual environment:

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r configs/examples/requirements-host.lock
python -m pip install --no-deps -e ./science -e ./routes/library-first -e ./routes/ligand-first -e ./routes/de-novo
python -m pip check
```

Inspect `science/pyproject.toml` and `configs/examples/requirements-host.lock` for the tested dependency constraints. Scientific engines have separate environments. Installing these Python packages does not install or license those engines.

## Agent host and web workspace

Use Bun 1.3.14 and follow `docs/development.md` for build commands. From `openscience-custom/`:

```sh
bun install --frozen-lockfile --ignore-scripts
```

Use your own provider credentials and execution configuration outside the source tree. Do not copy historical runtime identities, deployment paths or credentials from another installation. Start with `configs/examples/`; review all path and backend settings before starting services. Some deployment scripts still have site-specific assumptions and are not generic installers.

## Scientific engines

CovDock requires a separately licensed Schrödinger installation. Other scientific engines, weights, input structures, datasets, HPC connectivity and scheduler configuration must be provisioned separately. Ligand-first does not use CarsiDock-Cov; Library-first has its own policy in AGENTS.md. This release does not enable MD. The hypothesis-retrieval generator and project scientific fixtures/structures are intentionally not distributed; configure separately licensed replacements and external inputs before running those paths.

Offline source checks are possible without a model account, HPC credentials or a commercial engine. Scientific integration tests may require explicitly configured external fixtures/runtimes; skipped or unavailable integration coverage must be reported.
