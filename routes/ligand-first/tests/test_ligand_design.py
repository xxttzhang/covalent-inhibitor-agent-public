import json
import os
from pathlib import Path

import pytest

from test_native_case import acquisition, prepare as native_prepare
from covscreen.ligand_design import (
    POLICY, FULL_PARENT_POLICY, upstream_assets, design_folder, generate, inspect, prepare, protection, seal, write, plan_pose, deliver,
)
from covscreen.ligand_first_inputs import load_campaign, build_parent_pose_mol
from covscreen.target_hypothesis import digest


def test_pair_review_replays_coordinates_and_detects_new_site_connection(acquisition):
    from covscreen.ligand_parent_pair import review_acquisition
    from covscreen.parent_chemical_identity import audit_parent_chemical_identity

    record_path = acquisition / 'structure_acquisition.json'
    record = json.loads(record_path.read_text())
    (acquisition / 'cys_mapping.json').write_text(json.dumps(record['mappings'][0]))
    record['parent_chemical_identity'] = audit_parent_chemical_identity(
        acquisition / 'component.cif', record['coordinate_audit'],
        record['plan']['expected_inchikey'])
    record_path.write_text(json.dumps(record))
    assessment = review_acquisition(acquisition)
    assert assessment['status'] == 'ELIGIBLE_FOR_ENUMERATION_REVIEW'
    assert 5.9 < assessment['sg_ligand_min_distance_angstrom'] < 6.0
    assert assessment['execution_ready'] is False

    # Synthetic connection in a real-coordinate fixture: replay must inspect
    # source records, not accept the previously saved clean coordinate audit.
    line = list(' ' * 80)
    for start, value in [(0, 'SSBOND'), (15, 'A'), (17, ' 773'),
                         (29, 'B'), (31, '  42')]:
        line[start:start + len(value)] = value
    pdb = acquisition / 'structure.pdb'
    pdb.write_text(''.join(line) + '\n' + pdb.read_text())
    assessment = review_acquisition(acquisition)
    assert assessment['status'] == 'EVIDENCE_INSUFFICIENT'
    assert 'cys_connection_requires_review' in assessment['reasons']


@pytest.fixture
def legacy_pose_engine(monkeypatch):
    # Software-only export/plan regression; no scientific worker is started.
    # The fixture restores policy after each test. Native VS tests keep the
    # actual disabled-CarsiDock policy throughout.
    from covscreen.docking_policy import POLICY as docking_policy
    monkeypatch.setitem(docking_policy, 'disabled_engines', [])


@pytest.fixture
def design(acquisition, tmp_path):
    identifier = "design-" + "a" * 24
    out = design_folder(tmp_path, identifier)
    out.mkdir(parents=True)
    native_prepare(acquisition, out / "case")
    campaign = load_campaign(out / "case/campaign.json")
    parent = build_parent_pose_mol(campaign, out / "case")
    protected = protection(parent, campaign, out / "case")
    campaign = campaign.model_copy(update={"planning_mode": "rule-derived-v1",
        "allowed_warheads": POLICY["warheads"], "protection": campaign.protection.model_copy(update={
            key: protected[key] for key in ("scaffold_core_atom_names", "protected_pharmacophore_atom_names")})})
    campaign_data = campaign.model_dump(mode="json")
    campaign_data["protection"]["protected_interactions"] = protected["protected_interactions"]
    campaign = type(campaign).model_validate(campaign_data)
    (out / "case/campaign.json").write_text(campaign.model_dump_json())
    evidence_id = "hyp-" + "b" * 24
    evidence = tmp_path / "results/target-hypotheses" / evidence_id
    evidence.mkdir(parents=True)
    write(evidence / "evidence.json", {"literature": [], "structures": [], "existing_therapies": [],
        "fixture": "Synthetic lineage with real PDB/CCD coordinates; not scientific acceptance"})
    seal(evidence)
    write(out / "design.json", {"design_id": identifier, "evidence_id": evidence_id,
        "policy": POLICY,
        "state": "DESIGN_INPUTS_READY", "evidence_manifest_sha256": digest(evidence / "manifest.json")})
    seal(out)
    return identifier


@pytest.fixture
def graph_design(design, tmp_path):
    # Isolate the graph-worker contract with an explicitly ring-only test plan.
    # The production N/O-contact policy is tested separately below, including HOLD.
    out = design_folder(tmp_path, design)
    campaign = load_campaign(out / "case/campaign.json")
    campaign = campaign.model_copy(update={"protection": campaign.protection.model_copy(
        update={"protected_pharmacophore_atom_names": []})})
    (out / "case/campaign.json").write_text(campaign.model_dump_json())
    seal(out)
    return design


def test_real_graph_generation_rechecks_saved_sdf_and_is_idempotent(graph_design, tmp_path):
    design = graph_design
    first = generate(tmp_path, design)
    assert first["state"] == "NEEDS_POSE_EVALUATION"
    assert 0 < first["candidate_count"] <= POLICY["max_candidates"]
    assert first["graph_review_passed"] is True
    assert first["compute_submitted"] is False
    assert first["derivatization_feasibility"] == "unknown"
    assert generate(tmp_path, design) == first
    out = design_folder(tmp_path, design) / "generation"
    reviews = json.loads((out / "review.json").read_text())["candidates"]
    assert all(row["pose_assessment"] == "NOT_RUN" for row in reviews)
    rows = json.loads((out / "candidates.json").read_text())["candidates"]
    assert {row["warhead_name"] for row in rows} == {"acrylamide"}


@pytest.fixture
def full_parent_design(design, tmp_path, monkeypatch):
    repository = os.environ.get('COVALENTIZER_TEST_REPO')
    if not repository:
        pytest.skip('Explicit upstream checkout required for native generation integration')
    monkeypatch.setenv('COVALENTIZER', repository)
    directory = design_folder(tmp_path, design)
    campaign = load_campaign(directory / 'case/campaign.json')
    campaign = campaign.model_copy(update={'planning_mode': 'upstream-native'})
    (directory / 'case/campaign.json').write_text(campaign.model_dump_json())
    data = json.loads((directory / 'design.json').read_text())
    data.update(policy=FULL_PARENT_POLICY, upstream_assets=upstream_assets())
    write(directory / 'design.json', data)
    seal(directory)
    return design


def test_full_parent_native_generation_preserves_protection_and_delivers_evidence(full_parent_design, tmp_path):
    result = generate(tmp_path, full_parent_design)
    assert result['state'] == 'NEEDS_POSE_EVALUATION'
    assert result['candidate_count'] == 4
    assert result['graph_review_passed'] and not result['compute_submitted']
    assert generate(tmp_path, full_parent_design) == result
    directory = design_folder(tmp_path, full_parent_design)
    campaign = load_campaign(directory / 'case/campaign.json')
    assert set(campaign.protection.protected_pharmacophore_atom_names) == {'N2', 'O2', 'O4'}
    review = json.loads((directory / 'generation/upstream-review.json').read_text())
    assert len(review['inventory']) == review['source_candidate_count'] == 108
    candidates = json.loads((directory / 'generation/candidates.json').read_text())['candidates']
    assert all(row['provenance'] == 'upstream-covalentizer-full-parent-v1' for row in candidates)
    delivered = deliver(tmp_path, full_parent_design)
    assert delivered['delivery']['outcome'] == 'INCOMPLETE'
    assert '完整母体新增连接' in delivered['delivery']['scope']
    assert len(delivered['delivery']['candidate_selection']['selected_candidate_ids']) == 4
    assert all(not row['geometry']['direction_is_experimental'] for row in delivered['delivery']['site_details'])
    assert deliver(tmp_path, full_parent_design) == delivered


def test_full_parent_generation_rejects_asset_changes(full_parent_design, tmp_path):
    directory = design_folder(tmp_path, full_parent_design)
    data = json.loads((directory / 'design.json').read_text())
    data['upstream_assets']['files']['Code/CovUtils.py'] = '0' * 64
    write(directory / 'design.json', data)
    seal(directory)
    with pytest.raises(ValueError, match='assets differ'):
        generate(tmp_path, full_parent_design)
    assert not (directory / 'generation').exists()


def test_current_policy_still_blocks_carsidock(graph_design, tmp_path):
    from covscreen.docking_policy import POLICY as docking_policy
    assert 'carsidock-cov' in docking_policy['disabled_engines']
    generate(tmp_path, graph_design)
    with pytest.raises(ValueError, match='docking_engine_disabled'):
        plan_pose(tmp_path, graph_design)
    assert not (design_folder(tmp_path, graph_design) / 'pose-plan').exists()


def test_workflow_dispatches_full_parent_generation_without_docking(full_parent_design, tmp_path):
    from covscreen.ligand_workflow import DESIGN, navigate, step, directory
    initial = navigate(tmp_path, 'start', request_id='full-parent-interface-replay',
                       target='EGFR', site='C797', taxon=9606)
    identity = initial['workflow']['workflow_id']
    state_path = directory(tmp_path, identity) / 'state.json'
    state = json.loads(state_path.read_text())
    state.update(stage='DESIGN_INPUTS_READY', design_id=full_parent_design,
                 evidence_id='hyp-' + 'b' * 24)
    state_path.write_text(json.dumps(state))
    result = step(tmp_path, DESIGN, workflow_id=identity,
                  expected_revision=state['revision'], action='generate')
    assert result['workflow']['stage'] == 'NEEDS_POSE_EVALUATION'
    assert result['result']['candidate_count'] == 4
    assert result['execution']['state'] == 'not_submitted'
    saved = inspect(tmp_path, full_parent_design)
    assert saved['next_call'] is None
    assert 'prepare_docking_input' in saved['next_gate']
    assert 'direct CovDock-LO' in saved['next_gate']
    from covscreen.ligand_workflow import ENTRY
    delivered = step(tmp_path, DESIGN, workflow_id=identity,
                     expected_revision=result['workflow']['revision'], action='deliver')
    assert DESIGN not in delivered['workflow']['tools']
    reopened = navigate(tmp_path, 'reopen_design', workflow_id=identity,
        expected_revision=delivered['workflow']['revision'], reason='Continue the verified graph design')
    assert reopened['workflow']['stage'] == 'NEEDS_POSE_EVALUATION'
    assert 'prepare_vs_input' in reopened['workflow']['tools'][DESIGN]['actions']
    assert not reopened['compute_submitted'] if 'compute_submitted' in reopened else not reopened['result']['compute_submitted']




def test_conservative_contacts_can_reject_all_sites_without_claiming_target_infeasibility(design, tmp_path):
    result = generate(tmp_path, design)
    assert result["state"] == "NO_GRAPH_CANDIDATES"
    assert result["candidate_count"] == 0
    assert result["failure_count"] > 0
    assert result["derivatization_feasibility"] == "unknown"
    assert "do not register a pose job" in result["next_gate"]
    with pytest.raises(ValueError):
        plan_pose(tmp_path, design)


def test_generated_artifact_tampering_is_rejected(design, tmp_path):
    result = generate(tmp_path, design)
    Path(result["artifacts"]["candidates"]).write_text("forged")
    with pytest.raises(ValueError, match="Design artifact changed"):
        inspect(tmp_path, design)


def test_preparation_tampering_cannot_start_generation(design, tmp_path):
    (design_folder(tmp_path, design) / "case/campaign.json").write_text("{}")
    with pytest.raises(ValueError, match="Design artifact changed"):
        generate(tmp_path, design)


def test_new_evidence_version_requires_new_design(design, tmp_path):
    evidence = tmp_path / "results/target-hypotheses" / ("hyp-" + "b" * 24)
    write(evidence / "successor.json", {"evidence_id": "hyp-" + "c" * 24})
    with pytest.raises(ValueError, match="STALE"):
        generate(tmp_path, design)


def test_model_cannot_supply_arbitrary_design_paths(tmp_path):
    with pytest.raises(ValueError, match="Invalid design"):
        inspect(tmp_path, "../../outside")
    with pytest.raises(ValueError, match="proposal ID"):
        prepare(tmp_path, "hyp-" + "a" * 24, "../../outside", "a" * 64)


def test_empty_scaffold_is_not_a_rule_derived_plan(acquisition, tmp_path):
    native_prepare(acquisition, tmp_path / "case")
    data = json.loads((tmp_path / "case/campaign.json").read_text())
    data["planning_mode"] = "rule-derived-v1"
    from covscreen.ligand_first_inputs import LigandFirstCampaignSpec
    with pytest.raises(ValueError, match="protected scaffold"):
        LigandFirstCampaignSpec.model_validate(data)


@pytest.mark.usefixtures("legacy_pose_engine")
def test_pose_export_binds_exact_candidates_atoms_and_budget(graph_design, tmp_path):
    import csv
    from rdkit import Chem
    from covscreen.jobs import JobSubmissionRequest
    from covscreen.rdkit_covalentizer import WARHEAD_ATOM_MAPS
    generated = generate(tmp_path, graph_design)
    result = plan_pose(tmp_path, graph_design)
    assert result["state"] == "POSE_PLAN_READY"
    assert not result["compute_submitted"]
    assert result["runtime_registration"] == "NOT_RUN"
    assert plan_pose(tmp_path, graph_design) == result
    out = design_folder(tmp_path, graph_design) / "pose-plan"
    request = JobSubmissionRequest.model_validate_json((out / "job-request.json").read_text())
    assert request.execution_mode == "plan" and not request.human_approved
    assert request.resource_budget.gpu_count == 1
    assert request.resource_budget.max_runtime_minutes == 30
    assert request.parameters["covres"] == "A:CYS:773"  # Real 1M17 author mapping, not UniProt 797.
    rows = list(csv.DictReader((out / "ligands.csv").open()))
    molecules = list(Chem.SDMolSupplier(str(out / "ligands.sdf")))
    assert 0 < len(rows) == len(molecules) == result["pose_candidate_count"] <= 3
    assert result["deferred_candidate_count"] == generated["candidate_count"] - len(rows)
    for row, molecule in zip(rows, molecules):
        assert int(row["input_sdf_index"]) == molecule.GetIntProp("INPUT_SDF_INDEX")
        assert row["compound_id"] == molecule.GetProp("candidate_id") == molecule.GetProp("compound_id")
        assert int(row["electrophilic_atom_map"]) == WARHEAD_ATOM_MAPS["reaction_beta_carbon"]
    campaign = load_campaign(design_folder(tmp_path, graph_design) / "case/campaign.json")
    assert all(c.protein_uniprot_residue_number is None for c in campaign.protection.protected_interactions)
    (out / "ligands.csv").write_text("forged")
    with pytest.raises(ValueError, match="Design artifact changed"):
        plan_pose(tmp_path, graph_design)


@pytest.mark.usefixtures("legacy_pose_engine")
def test_no_graph_candidates_cannot_plan_compute(design, tmp_path):
    generate(tmp_path, design)
    with pytest.raises(ValueError, match="nonempty graph"):
        plan_pose(tmp_path, design)


@pytest.mark.usefixtures("legacy_pose_engine")
def test_missing_contacts_remains_blocked_not_vacuous_pass(graph_design, tmp_path):
    directory = design_folder(tmp_path, graph_design)
    campaign = load_campaign(directory / "case/campaign.json")
    campaign = campaign.model_copy(update={"protection": campaign.protection.model_copy(
        update={"protected_interactions": []})})
    (directory / "case/campaign.json").write_text(campaign.model_dump_json())
    seal(directory)
    generate(tmp_path, graph_design)
    result = plan_pose(tmp_path, graph_design)
    assert result["state"] == "POSE_INPUTS_BLOCKED" and result["scientific_blockers"]
    assert not result["compute_submitted"]


def test_negative_delivery_keeps_contact_and_ring_reasons_distinct(design, tmp_path):
    generate(tmp_path, design)
    result = deliver(tmp_path, design)
    disposition = result["delivery"]
    assert disposition["outcome"] == "BOUNDED_NEGATIVE_BRANCH"
    assert not disposition["full_computational_loop_accepted"]
    assert disposition["pose_evaluation"] == "NOT_RUN"
    atoms = [atom for site in disposition["site_details"] for atom in site["protected_removed_atoms"]]
    assert atoms
    contacts = [atom for atom in atoms if not atom["is_ring"]]
    assert contacts
    assert all("受保护环骨架" not in atom["protection_reasons"] for atom in contacts)
    assert deliver(tmp_path, design) == result
    assert result["next_call"] is None


def test_graph_pass_is_not_delivered_as_completed_computational_acceptance(graph_design, tmp_path):
    generate(tmp_path, graph_design)
    result = deliver(tmp_path, graph_design)
    assert result["delivery"]["outcome"] == "INCOMPLETE"
    assert not result["delivery"]["full_computational_loop_accepted"]


def test_policy_upgrade_cannot_silently_change_prepared_design(graph_design, tmp_path):
    out = design_folder(tmp_path, graph_design)
    data = json.loads((out / "design.json").read_text())
    data["policy"]["max_sites"] = 1
    write(out / "design.json", data)
    seal(out)
    with pytest.raises(ValueError, match="policy differs"):
        generate(tmp_path, graph_design)


def test_workflow_negative_generation_delivery_and_parent_reconsideration(design, tmp_path):
    """Replay a prepared-state boundary with real PDB/CCD and the production generator.

    The fixture does not claim end-to-end parent evidence acceptance; that gate
    is tested independently. No scientific tool is mocked in this replay.
    """
    from covscreen.ligand_workflow import ENTRY, EVIDENCE, DESIGN, navigate, step, directory
    initial = navigate(tmp_path, "start", request_id="negative-design-replay", target="EGFR", site="C797", taxon=9606)
    identity = initial["workflow"]["workflow_id"]
    path = directory(tmp_path, identity) / "state.json"
    prepared = json.loads(path.read_text())
    prepared.update(stage="DESIGN_INPUTS_READY", design_id=design, evidence_id="hyp-" + "b" * 24)
    path.write_text(json.dumps(prepared))
    generated = step(tmp_path, DESIGN, workflow_id=identity, expected_revision=prepared["revision"], action="generate")
    assert generated["workflow"]["stage"] == "NO_GRAPH_CANDIDATES"
    assert generated["result"]["candidate_count"] == 0
    assert generated["assessment"]["scope"] == "graph_generation"
    assert generated["assessment"]["state"] == "rejected"
    assert generated["execution"]["state"] == "not_submitted"
    assert generated["workflow"]["tools"][DESIGN]["actions"] == ["deliver"]
    assert "supplement" in generated["workflow"]["tools"][EVIDENCE]["actions"]
    assert "reconsider" in generated["workflow"]["tools"][ENTRY]["actions"]
    with pytest.raises(ValueError, match="unavailable"):
        step(tmp_path, DESIGN, workflow_id=identity, expected_revision=generated["workflow"]["revision"], action="plan_pose")
    delivered = step(tmp_path, DESIGN, workflow_id=identity, expected_revision=generated["workflow"]["revision"], action="deliver")
    assert delivered["workflow"]["stage"] == "BRANCH_REPORTED"
    assert DESIGN not in delivered["workflow"]["tools"]
    with pytest.raises(ValueError, match='VS planning requires'):
        step(tmp_path, DESIGN, workflow_id=identity,
             expected_revision=delivered['workflow']['revision'], action='prepare_vs_input')
    with pytest.raises(ValueError, match='No resumable design'):
        navigate(tmp_path, 'reopen_design', workflow_id=identity,
                 expected_revision=delivered['workflow']['revision'], reason='Attempt to resume zero candidates')
    assert not delivered["result"]["delivery"]["full_computational_loop_accepted"]
    disposition = design_folder(tmp_path, design) / "delivery/disposition.json"
    before = digest(disposition)
    reopened = navigate(tmp_path, "reconsider", workflow_id=identity,
        expected_revision=delivered["workflow"]["revision"], reason="Compare another parent after the bounded graph branch yielded no candidates")
    assert reopened["workflow"]["stage"] == "PARENT_REVIEW"
    assert reopened["design_id"] is None
    assert "finalize" in reopened["workflow"]["tools"][EVIDENCE]["actions"]
    assert reopened["branches"][0]["design_id"] == design
    previous = navigate(tmp_path, "inspect_branch", workflow_id=identity,
        expected_revision=reopened["workflow"]["revision"], branch_index=0)
    assert previous["result"]["record"]["delivery"]["outcome"] == "BOUNDED_NEGATIVE_BRANCH"
    assert previous["result"]["active_handoff_restored"] is False
    assert digest(disposition) == before


@pytest.mark.usefixtures("legacy_pose_engine")
def test_workflow_pose_export_remains_incomplete_and_keeps_evidence_and_reselection(graph_design, tmp_path):
    """Graph-worker fixture with real coordinates; not a production parent acceptance claim."""
    from covscreen.ligand_workflow import ENTRY, EVIDENCE, DESIGN, navigate, step, directory
    initial = navigate(tmp_path, "start", request_id="pose-interface-replay", target="EGFR", site="C797", taxon=9606)
    identity = initial["workflow"]["workflow_id"]
    path = directory(tmp_path, identity) / "state.json"
    state = json.loads(path.read_text())
    state.update(stage="DESIGN_INPUTS_READY", design_id=graph_design, evidence_id="hyp-" + "b" * 24)
    path.write_text(json.dumps(state))
    generated = step(tmp_path, DESIGN, workflow_id=identity, expected_revision=state["revision"], action="generate")
    assert generated["workflow"]["stage"] == "NEEDS_POSE_EVALUATION"
    assert {"plan_pose", "deliver"}.issubset(generated["workflow"]["tools"][DESIGN]["actions"])
    planned = step(tmp_path, DESIGN, workflow_id=identity,
        expected_revision=generated["workflow"]["revision"], action="plan_pose")
    assert planned["workflow"]["stage"] == "POSE_PLAN_READY"
    assert {"check_pose_runtime", "deliver"}.issubset(planned["workflow"]["tools"][DESIGN]["actions"])
    assert planned["result"]["runtime_registration"] == "NOT_RUN"
    assert not planned["compute_submitted"]
    assert planned["execution"]["state"] == "not_submitted"
    assert planned["assessment"]["state"] == "not_evaluated"
    assert planned["blockers"][0]["code"] == "pose_evidence_required"
    assert planned["blockers"][0]["recoverable"] is True
    assert "measure_parent" in planned["workflow"]["tools"][EVIDENCE]["actions"]
    delivered = step(tmp_path, DESIGN, workflow_id=identity,
        expected_revision=planned["workflow"]["revision"], action="deliver")
    assert delivered["workflow"]["stage"] == "BRANCH_REPORTED"
    assert delivered["result"]["delivery"]["outcome"] == "INCOMPLETE"
    assert "reconsider" in delivered["workflow"]["tools"][ENTRY]["actions"]
    assert "supplement" in delivered["workflow"]["tools"][EVIDENCE]["actions"]
    assert "finalize" not in delivered["workflow"]["tools"][EVIDENCE]["actions"]


@pytest.mark.usefixtures("legacy_pose_engine")
def test_pose_runtime_compiles_existing_executor_without_registering(graph_design, tmp_path):
    from covscreen.ligand_workflow import DESIGN, navigate, step, directory
    generate(tmp_path, graph_design)
    plan_pose(tmp_path, graph_design)
    config = tmp_path / 'configs'
    config.mkdir()
    repo = tmp_path / 'fixture-engine'
    repo.mkdir()
    checkpoint = repo / 'fixture.pt'
    checkpoint.write_text('Not a scientific checkpoint; compilation-only fixture')
    (config / 'local.inspire.yaml').write_text(json.dumps({
        'runtime': {'enable_ligand_smoke_dispatch': True},
        'carsidock': {'repo': str(repo), 'checkpoint': str(checkpoint)}}))
    initial = navigate(tmp_path, "start", request_id="runtime-check", target="EGFR", site="C797", taxon=9606)
    workflow_id = initial["workflow"]["workflow_id"]
    path = directory(tmp_path, workflow_id) / "state.json"
    state = json.loads(path.read_text())
    state.update(stage="POSE_PLAN_READY", design_id=graph_design, evidence_id="hyp-" + "b" * 24)
    write(path, state)
    response = step(tmp_path, DESIGN, workflow_id=workflow_id,
                    expected_revision=state["revision"], action="check_pose_runtime")
    assert response["workflow"]["stage"] == "POSE_PLAN_READY"
    assert response["execution"]["state"] == "not_submitted"
    assert response["assessment"]["state"] == "not_evaluated"
    result = response["result"]
    spec = result['compiled_spec']
    assert result['runtime_check'] == 'registered_plan_compiled'
    assert not result['compute_submitted'] and spec['execution_mode'] == 'plan'
    assert spec['provenance']['worker_backend'] == 'external-inspire'
    assert spec['parameters']['covres'] == 'A:CYS:773'
    assert spec['parameters']['docking_binding'] in spec['input_sha256']
    assert not list((tmp_path / 'state/jobs').glob('*/spec.json'))
    frozen = response["pose_execution"]
    assert frozen["request_id"].startswith(workflow_id + "-pose-r")
    repeated = step(tmp_path, DESIGN, workflow_id=workflow_id,
                    expected_revision=response["workflow"]["revision"], action="check_pose_runtime")
    assert repeated["pose_execution"] == frozen
    from covscreen.ligand_pose_execution import frozen_request
    assert frozen_request(tmp_path, json.loads(path.read_text())).request_id == frozen["request_id"]
    persisted = json.loads(path.read_text())
    with pytest.raises(ValueError, match="pending workflow operation"):
        frozen_request(tmp_path, {**persisted, "pending": {"operation_id": "lost-reply"}})
    with pytest.raises(ValueError, match="current workflow design and evidence"):
        frozen_request(tmp_path, {**persisted, "stage": "BRANCH_REPORTED"})
    with pytest.raises(ValueError, match="current workflow design and evidence"):
        frozen_request(tmp_path, {**persisted, "evidence_id": "hyp-" + "c" * 24})
    checkpoint.write_text("Changed fixture checkpoint after plan freezing")
    with pytest.raises(ValueError, match="Frozen pose runtime plan changed"):
        step(tmp_path, DESIGN, workflow_id=workflow_id,
             expected_revision=repeated["workflow"]["revision"], action="check_pose_runtime")
    after_failure = navigate(tmp_path, "status", workflow_id=workflow_id)
    assert after_failure["pose_execution"] == frozen
    assert after_failure["pending"] is None
    reconsidered = navigate(tmp_path, "reconsider", workflow_id=workflow_id,
        expected_revision=after_failure["workflow"]["revision"],
        reason="Reassess the parent before compiling against a different runtime")
    assert reconsidered["pose_execution"] is None
    assert reconsidered["branches"][-1]["pose_execution"] == frozen


@pytest.mark.parametrize('change', ['budget', 'request', 'selection', 'lineage'])
@pytest.mark.usefixtures("legacy_pose_engine")
def test_pose_runtime_rejects_resealed_export_changes(graph_design, tmp_path, change):
    from covscreen.ligand_pose_execution import bound_request
    generate(tmp_path, graph_design)
    plan_pose(tmp_path, graph_design)
    plan = design_folder(tmp_path, graph_design) / 'pose-plan'
    filename = 'job-request.json' if change in {'budget', 'request'} else 'input-binding.json'
    path = plan / filename
    data = json.loads(path.read_text())
    if change == 'budget':
        data['resource_budget']['max_runtime_minutes'] = 60
    elif change == 'request':
        data['request_id'] = 'substitute-request'
    elif change == 'selection':
        data['selected_candidate_ids'] = ['substitute-candidate']
    else:
        data['generation_manifest_sha256'] = '0' * 64
    write(path, data)
    seal(plan)
    with pytest.raises(ValueError, match='policy|selection|lineage'):
        bound_request(tmp_path, graph_design)


@pytest.mark.usefixtures("legacy_pose_engine")
def test_pose_runtime_requires_explicit_dispatch_configuration(graph_design, tmp_path):
    from covscreen.ligand_pose_execution import check_runtime
    generate(tmp_path, graph_design)
    plan_pose(tmp_path, graph_design)
    with pytest.raises(ValueError, match='dispatch is not enabled'):
        check_runtime(tmp_path, graph_design)


@pytest.mark.usefixtures("legacy_pose_engine")
def test_pose_runtime_rejects_resealed_coordinate_substitution(graph_design, tmp_path):
    from covscreen.ligand_pose_execution import bound_request
    from rdkit import Chem
    generate(tmp_path, graph_design)
    plan_pose(tmp_path, graph_design)
    plan = design_folder(tmp_path, graph_design) / 'pose-plan'
    molecules = list(Chem.SDMolSupplier(str(plan / 'ligands.sdf'), removeHs=False))
    point = molecules[0].GetConformer().GetAtomPosition(0)
    molecules[0].GetConformer().SetAtomPosition(0, (point.x + 1, point.y, point.z))
    with Chem.SDWriter(str(plan / 'ligands.sdf')) as writer:
        for molecule in molecules:
            writer.write(molecule)
    seal(plan)
    with pytest.raises(ValueError, match='structure changed'):
        bound_request(tmp_path, graph_design)
