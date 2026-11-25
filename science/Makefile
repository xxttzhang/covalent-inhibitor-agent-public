PYTHON ?= python

.PHONY: setup check-env prepare-covalid btk-mini prepare-library split chunk-sdfs smoke-carsi merge-scores metrics af3-smoke parse-af3 parse-af3-batch covdock-inputs ligprep covdock-smoke parse-covdock validate-workflow fuse-rankings test report btk-full full-covalid

setup:
	$(PYTHON) -m pip install -e .

check-env:
	$(PYTHON) scripts/check_environment.py --json-out reports/environment_report.json

prepare-covalid:
	$(PYTHON) scripts/prepare_covalid.py

btk-mini:
	$(PYTHON) scripts/prepare_btk_mini.py

prepare-library:
	$(PYTHON) ../routes/library-first/scripts/prepare_library.py

split:
	$(PYTHON) ../routes/library-first/scripts/split_library.py

chunk-sdfs:
	$(PYTHON) scripts/make_chunk_sdfs.py

smoke-carsi:
	@echo "Use scripts/run_chunk.py with explicit receptor, reference ligand, chunk and --cuda-device-index."

merge-scores:
	$(PYTHON) scripts/merge_scores.py

metrics:
	@test -n "$(SCORE_DIRECTION)" || (echo "Set SCORE_DIRECTION after validating the engine score semantics"; exit 2)
	$(PYTHON) scripts/compute_metrics.py --score-direction $(SCORE_DIRECTION)

af3-smoke:
	@echo "Use scripts/run_af3_job.py with a generated JSON and an explicit physical GPU."

parse-af3:
	@echo "Use scripts/parse_af3_results.py with the AF3 input manifest."

parse-af3-batch:
	@echo "Use scripts/parse_af3_batch.py with the AF3 batch manifests and result root."

covdock-inputs:
	@echo "Use scripts/prepare_covdock_inputs.py; this does not check out a license."

ligprep:
	@echo "Use scripts/run_ligprep_job.py with the official institutional license file."

covdock-smoke:
	@echo "Requires a clean institutional SCHROD_LICENSE_FILE; run scripts/run_covdock_job.py."

parse-covdock:
	@echo "Inspect pose properties first, then use scripts/parse_covdock_scores.py with an explicit verified score field."

validate-workflow:
	@echo "Use scripts/validate_btk_workflow.py after engine tables are materialized."

fuse-rankings:
	@echo "Use scripts/fuse_rankings.py with explicit engine score columns and directions."

test:
	pytest -q

report:
	$(PYTHON) scripts/generate_report.py

btk-full:
	@echo "Explicitly disabled in the mini validation workflow."; exit 2

full-covalid:
	@echo "Explicitly disabled in the mini validation workflow."; exit 2
