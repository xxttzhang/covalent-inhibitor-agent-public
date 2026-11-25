# Upstream versions and verified status

> 发布说明：本文历史部署路径已匿名化；`/path/to/` 与 `<account>` 是占位符，实际路径由部署方在源码外配置。历史验收不代表当前部署状态。

Initially verified on 2026-07-27 and migrated to the replacement `inspire`
runtime on 2026-08-17.

## CarsiDock-Cov

- Repository: `/inspire/hdd/global_user/<account>/1_zhang/CarsiDock-Cov`
- Commit: `4fc87c4ee3aac73e6ce00da3d97286c22deebd43`
- Repository source is unchanged. The only proposed compatibility change is
  stored in `environment/compatibility.patch` and is not applied without
  explicit approval.
- CarsiDock checkpoint SHA256:
  `f6e0c7c1e47c6c60bb6d88b33c57c562c7a6020cd93168ca9c2d385e3d7c91b6`
- RTMScore checkpoint SHA256:
  `399727a85de96c011a75a4c242ebe947753724f01e8d15139fae7bcad106bfc2`
- GPU environment: Python 3.11.15, PyTorch 2.4.0+cu124, DGL 2.4.0+cu124,
  RDKit 2022.09.4.
- The replacement node did not provide the former container-local
  `/opt/conda/bin/python`. A persistent Python 3.11 runtime now lives at
  `/inspire/hdd/global_user/<account>/2_agent/python311_runtime`;
  `carsidock_gpu_env/bin/python` points to that shared runtime. This preserves
  the existing CPython 3.11 site-packages while making the venv portable across
  new Inspire containers.
- Verified hardware: two NVIDIA GeForce RTX 4090 devices.

The upstream RTMScore code creates one batch-index tensor on CPU while the
mask is on GPU under this PyTorch/DGL stack. Docking completes, but rescoring
raises a device mismatch. The recorded one-line patch only places that tensor
on the mask device; it does not change model calculations.

## AlphaFold 3

- Repository: `/inspire/hdd/global_user/<account>/git_develop/af3`
- Commit: `f3e86f27dfac16559d16f470bb2f9323eb357f1f`
- Python environment:
  `/inspire/hdd/global_user/<account>/conda_envs/af3_native`
- Python 3.12.13, JAX 0.4.34, jaxlib 0.4.34.
- Model directory:
  `/inspire/hdd/global_user/<account>/git_develop/af3/models`
- Database directory:
  `/inspire/hdd/global_user/<account>/public_databases`
- A real BTK–B1852 protein–ligand cofolding run completed successfully.

The AF3 working tree already contains local changes and generated assets that
pre-date this project. The commit and dirty-state audit must therefore remain
part of every run manifest.

## Schrödinger CovDock

- Installation:
  `/inspire/hdd/global_user/<account>/0_software/schrodinger2021-2`
- Version: Schrödinger Suite 2021-2.
- Official `covalent_docking -h`, `structconvert -h`, and `prepwizard -h`
  were inspected to freeze the installed-version command syntax.
- BTK B1852 ligand and chain-A receptor were converted to Maestro format.
- Vendor-signed local-entitlement archive:
  `/inspire/hdd/global_user/<account>/2_agent/licenses.zip`
  (SHA256 `ae44e4125649901e3379071a2c1a322fa3b46c3e275ed6523cc23cd0171d6984`).
- Selectively extracted license:
  `/inspire/hdd/global_user/<account>/2_agent/schrodinger_official_license/25-4.lic`
  (SHA256 `cb90a42707dedf8626cf6080a6bbb3116a443787f453e995484137e11fb45558`).
- The signed entitlement was accepted by PrepWizard, LigPrep, Glide, and the
  CovDock pipeline. The older suspicious license directory remains prohibited.
- Isolated runtime compatibility directory:
  `/inspire/hdd/global_user/<account>/2_agent/schrodinger_runtime_libs/lib`
  contains only freetype/png/z symlinks needed by the 2021-2 LigPrep Python
  stack.
