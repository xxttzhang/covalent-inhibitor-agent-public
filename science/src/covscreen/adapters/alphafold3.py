from __future__ import annotations

from pathlib import Path

from .base import EngineAdapter


AF3_DATABASE_FILES = (
    "bfd-first_non_consensus_sequences.fasta",
    "mgy_clusters_2022_05.fa",
    "uniprot_all_2021_04.fa",
    "uniref90_2022_05.fa",
    "nt_rna_2023_02_23_clust_seq_id_90_cov_80_rep_seq.fasta",
    "rfam_14_9_clust_seq_id_90_cov_80_rep_seq.fasta",
    "rnacentral_active_seq_id_90_cov_80_linclust.fasta",
    "pdb_seqres_2022_09_28.fasta",
)


class AlphaFold3Adapter(EngineAdapter):
    name = "alphafold3"

    def __init__(
        self,
        runner: str,
        python: str,
        model_dir: str,
        database_dir: str,
    ):
        self.runner = Path(runner)
        self.python = Path(python)
        self.model_dir = Path(model_dir)
        self.database_dir = Path(database_dir)

    def check_availability(self) -> dict[str, object]:
        missing_databases = [
            name
            for name in AF3_DATABASE_FILES
            if not (self.database_dir / name).is_file()
        ]
        model_files = sorted(self.model_dir.glob("*.bin"))
        report = {
            "runner_exists": self.runner.is_file(),
            "python_exists": self.python.is_file(),
            "model_dir_exists": self.model_dir.is_dir(),
            "model_files": [str(path) for path in model_files],
            "database_dir_exists": self.database_dir.is_dir(),
            "missing_databases": missing_databases,
        }
        report["ready"] = bool(
            report["runner_exists"]
            and report["python_exists"]
            and model_files
            and not missing_databases
        )
        return report

    def build_command(
        self,
        json_path: str,
        output_dir: str,
        gpu_device: int = 0,
        num_recycles: int = 3,
        num_diffusion_samples: int = 1,
        num_seeds: int = 1,
        cpu_per_search: int = 2,
        jax_cache_dir: str | None = None,
        run_data_pipeline: bool = True,
    ) -> list[str]:
        input_path = Path(json_path)
        input_flag = (
            f"--input_dir={input_path}"
            if input_path.is_dir()
            else f"--json_path={input_path}"
        )
        command = [
            str(self.python),
            str(self.runner),
            input_flag,
            f"--model_dir={self.model_dir}",
            f"--db_dir={self.database_dir}",
            f"--output_dir={output_dir}",
            f"--gpu_device={gpu_device}",
            "--flash_attention_implementation=triton",
            f"--num_recycles={num_recycles}",
            f"--num_diffusion_samples={num_diffusion_samples}",
            f"--jackhmmer_n_cpu={cpu_per_search}",
            "--jackhmmer_max_parallel_shards=1",
            f"--nhmmer_n_cpu={cpu_per_search}",
            "--nhmmer_max_parallel_shards=1",
            "--force_output_dir=true",
            f"--jackhmmer_binary_path={self.python.parent / 'jackhmmer'}",
            f"--nhmmer_binary_path={self.python.parent / 'nhmmer'}",
            f"--hmmalign_binary_path={self.python.parent / 'hmmalign'}",
            f"--hmmbuild_binary_path={self.python.parent / 'hmmbuild'}",
            f"--hmmsearch_binary_path={self.python.parent / 'hmmsearch'}",
        ]
        if not run_data_pipeline:
            command.append("--run_data_pipeline=false")
        # AF3 interprets --num_seeds as a request to expand one input seed and
        # rejects an explicit value of 1.  For a one-seed smoke run, preserve
        # the seed in the JSON and omit this flag.
        if num_seeds > 1:
            command.append(f"--num_seeds={num_seeds}")
        if jax_cache_dir:
            command.append(f"--jax_compilation_cache_dir={jax_cache_dir}")
        return command

    def parse_output(self, output_dir: str | Path) -> dict[str, list[str]]:
        output_dir = Path(output_dir)
        return {
            "models": [str(p) for p in sorted(output_dir.rglob("*_model.cif"))],
            "summary_confidences": [
                str(p)
                for p in sorted(output_dir.rglob("*_summary_confidences.json"))
            ],
            "confidences": [
                str(p)
                for p in sorted(output_dir.rglob("*_confidences.json"))
                if not p.name.endswith("_summary_confidences.json")
            ],
            "ranking_scores": [
                str(p) for p in sorted(output_dir.rglob("*_ranking_scores.csv"))
            ],
        }

    def validate_output(self, parsed: dict[str, list[str]]) -> bool:
        return bool(parsed["models"] and parsed["summary_confidences"])
