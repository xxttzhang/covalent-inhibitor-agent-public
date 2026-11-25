"""Evidence-bound exploratory pose reports over the existing evaluator.

This adapter explains the current measurements; it neither changes the frozen
numerical policy nor upgrades legacy gates to calibrated physical acceptance.
"""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from .jobs import sha256_file
from .ligand_design import design_folder, seal, verify
from .ligand_first_inputs import load_campaign
from .receptor_evidence import regular
from .ligand_pose_evidence import incomplete


# Runtime projection of routes/METRIC_SEMANTICS.md for this evaluator only.
# Numerical criteria always come from the frozen plan, never this catalogue.
METRICS = {
    "scaffold_rmsd": {
        "metric_id": "protected_scaffold_displacement", "role": "task_constraint",
        "field": "scaffold_rmsd_angstrom", "units": "angstrom",
        "policy_field": "max_scaffold_rmsd_angstrom", "operator": "<=",
        "meaning": "指定母体核心原子在共同受体坐标系中的位移 RMSD。",
        "limitation": "没有单独最优叠合配体；仅检验本分支的母体保留要求，不证明活性。",
    },
    "interaction_retention": {
        "metric_id": "protected_contact_retention", "role": "task_constraint",
        "field": "interaction_retention_fraction", "units": "fraction",
        "policy_field": "min_interaction_retention_fraction", "operator": ">=",
        "meaning": "指定原子对满足距离条件的接触保留比例。",
        "limitation": "不是氢键或作用能保留；接触集合为空时不能评价。",
    },
    "covalent_cs_distance": {
        "metric_id": "product_bond_geometry", "role": "physical_validity_gate",
        "field": "covalent_cs_bond_distance_angstrom", "units": "angstrom",
        "policy_field": "covalent_cs_bond_window_angstrom", "operator": "inclusive_range",
        "meaning": "成键后对接表示中虚拟硫与反应碳的距离。",
        "limitation": "不是实际 SG—反应碳距离，必须结合实际 SG 指标；不证明反应前可达性。",
    },
    "attack_angle": {
        "metric_id": "product_bond_geometry", "role": "physical_validity_gate",
        "field": "attack_angle_s_cbeta_calpha_degrees", "units": "degree",
        "policy_field": "attack_angle_window_degrees", "operator": "inclusive_range",
        "meaning": "已连接虚拟硫的产物表示中的 S—Cβ—Cα 键角。",
        "limitation": "旧字段名含 attack_angle，但不测反应前进攻方向或反应能垒。",
    },
    "dummy_target_alignment": {
        "metric_id": "dummy_site_alignment", "role": "technical_qc",
        "field": "dummy_to_target_sg_distance_angstrom", "units": "angstrom",
        "policy_field": "max_dummy_to_target_sg_distance_angstrom", "operator": "<=",
        "meaning": "对接虚拟硫与指定受体 SG 的位置差。",
        "limitation": "仅为锚点表示一致性，不证明成键或抑制作用。",
    },
    "actual_target_cs_distance": {
        "metric_id": "product_bond_geometry", "role": "physical_validity_gate",
        "field": "actual_sg_cbeta_distance_angstrom", "units": "angstrom",
        "policy_field": "covalent_cs_bond_window_angstrom", "operator": "inclusive_range",
        "meaning": "受体中实际目标 SG 与映射反应碳的距离。",
        "limitation": "使用当前探索性产物键长窗口；不证明反应发生或反应前可达性。",
    },
    "actual_target_product_angle": {
        "metric_id": "product_bond_geometry", "role": "physical_validity_gate",
        "field": "actual_sg_cbeta_calpha_angle_degrees", "units": "degree",
        "policy_field": "attack_angle_window_degrees", "operator": "inclusive_range",
        "meaning": "受体实际目标 SG 与产物 Cβ、Cα 定义的键角。",
        "limitation": "沿用探索性产物角度窗口；不是反应前进攻方向或反应能垒。",
    },
    "ligand_strain": {
        "metric_id": "parent_relative_strain_change", "role": "risk_annotation",
        "field": "strain_excess_over_parent_kcal_mol", "units": "kcal/mol",
        "policy_field": "max_strain_excess_over_parent_kcal_mol", "operator": "<=",
        "meaning": "候选与母体各自单起点局部松弛能量差的差值。",
        "limitation": "不是完整构象应变或结合自由能；仅在局部最小化收敛且方法一致时可用，截断定义仍有限，不能替代候选绝对风险。",
    },
    "protein_clash": {
        "metric_id": "nonbonded_overlap", "role": "physical_validity_gate",
        "field": "clash_fraction", "units": "fraction",
        "policy_field": "max_clash_fraction", "operator": "<=",
        "meaning": "当前有限原子集合内发生所定义碰撞的配体重原子比例。",
        "limitation": "仅检查沉积结构中的蛋白重原子；跨成键 1-2/1-3/1-4 原子对另行处理，HETATM 环境未覆盖。",
    },
    "severe_protein_clash_absent": {
        "metric_id": "nonbonded_overlap", "role": "physical_validity_gate",
        "field": "severe_clash_pair_count", "units": "atom_pairs",
        "policy_field": "max_severe_clash_pair_count", "operator": "<=",
        "meaning": "按拓扑排除成键近邻后的严重蛋白—配体重原子冲突对数。",
        "limitation": "首版默认不允许严重冲突；不代表未检查的水、离子、辅因子及成键近邻均合理。",
    },
}
LIMITATIONS = [
    "本报告为探索性候选姿势报告；旧数值门槛的通过不等于完整物理质量验收。",
    "真实 SG 产物几何采用探索性窗口；反应前进攻几何、跨成键近邻及 HETATM 环境仍需独立证据。",
    "局部最小化未收敛或方法不一致时相对能量不可用；参考构象集合及加合物截断定义仍需完善。",
    "亲和力、反应动力学、选择性、合成可行性、MD 和实验活性均未由本报告验证。",
]


def measurement_rows(pose, policy):
    """Separate observed values, old gate labels and supported interpretations."""
    rows = []
    for gate, definition in METRICS.items():
        value = pose.get(definition["field"])
        available = value is not None and not (
            gate == "interaction_retention" and not pose["protected_interactions"])
        rows.append({
            **definition, "legacy_field": definition["field"],
            "measurement_status": "available" if available else "unavailable",
            "value": value if available else None,
            "criterion": {"operator": definition["operator"],
                          "value": policy.get(definition["policy_field"])},
            "gate_passed": pose["gate_flags"].get(gate),
            "clash_implementation": pose.get("clash_method", "legacy_whole_target_residue_excluded")
                if gate in {"protein_clash", "severe_protein_clash_absent"} else None,
            # The rules have not been calibrated as general scientific acceptance.
            "decision": "undetermined",
            "calibration_status": "exploratory_default",
        })
    return rows


def build(root, state):
    """Caller must freshly replay the signed pose job and its current outputs."""
    record = state["pose_execution"]
    if record.get("evaluation_verification") != "verified":
        raise ValueError("Pose report requires freshly revalidated registered evidence")
    evaluation = record["evaluation"]
    directory = design_folder(root, state["design_id"])
    campaign = load_campaign(directory / "case/campaign.json")
    policy_path = directory / "pose-plan/geometry-policy.json"
    policy = json.loads(policy_path.read_text())
    input_binding = json.loads((directory / "pose-plan/input-binding.json").read_text())
    generated = {candidate["candidate_id"]: candidate for candidate in
        json.loads((directory / "generation/candidates.json").read_text())["candidates"]}
    candidates = []
    for candidate in evaluation["candidates"]:
        if candidate["policy"] != policy:
            raise ValueError("Reported geometry policy differs from the frozen plan")
        candidates.append({
            "candidate_id": candidate["candidate_id"],
            "free_ligand": {key: generated[candidate["candidate_id"]][key] for key in (
                "canonical_smiles", "exit_vector_id", "warhead_name", "linker_name", "reaction_class")},
            "source_pose_record_indexes": candidate["source_pose_record_indexes"],
            "pose_count": candidate["pose_count"],
            "exploratory_gate_status": candidate["status"],
            "exploratory_passing_pose_count": candidate["passing_pose_count"],
            "selected_pose_record_index": candidate["selected_pose_record_index"],
            "assessment": ("review_required" if candidate.get("schema_version") == "2.0"
                           and candidate["assessment"]["review_eligible"] else
                           "evidence_insufficient" if incomplete(candidate) else "review_required"),
            **({"independent_axes": candidate["assessment"], "aggregation": candidate["aggregation"]}
               if candidate.get("schema_version") == "2.0" else {}),
            "errors": candidate["errors"],
            "poses": [{
                "source_pose_record_index": candidate["source_pose_record_indexes"][pose["pose_index"]],
                "exploratory_gate_status": pose["status"], "exploratory_failure_reasons": pose["failure_reasons"],
                **({"independent_axes": pose["assessment"], "strain_context": pose["strain_context"]}
                   if candidate.get("schema_version") == "2.0" else {}),
                "metrics": measurement_rows(pose, policy),
                "protected_contacts": pose["protected_interactions"],
                "local_relaxation_energy": {
                    "candidate_kcal_mol": pose["ligand_strain_kcal_mol"],
                    "parent_kcal_mol": pose["parent_ligand_strain_kcal_mol"],
                    "method": pose["strain_forcefield"],
                    "estimator": pose.get("strain_estimator", "legacy_unchecked"),
                    "convergence_verified": (
                        pose.get("strain_estimator") == "local_relaxation_convergence_checked_v1"
                        and pose["ligand_strain_kcal_mol"] is not None
                        and pose["parent_ligand_strain_kcal_mol"] is not None)},
                "clashes": {key: pose[key] for key in (
                    "severe_clash_pair_count", "clashing_ligand_atom_count", "ligand_heavy_atom_count",
                    "clash_method")},
                "scores": {key: pose[key] for key in ("carsidock_score", "pose_loss", "pose_mdn_score")},
            } for pose in candidate["poses"]],
        })
    from . import ligand_pose_evidence, ligand_first_geometry
    implementations = [ligand_pose_evidence, ligand_first_geometry]
    if evaluation["schema"] == "ligand-pose-geometry-review-v2":
        from covscreen_ligand_first import ligand_geometry_assessment
        implementations.append(ligand_geometry_assessment)
    payload = {
        "schema": ("ligand-exploratory-pose-report-v3" if evaluation["schema"] == "ligand-pose-geometry-review-v2"
                   else "ligand-exploratory-pose-report-v2"),
        "metric_semantics_version": "2" if evaluation["schema"] == "ligand-pose-geometry-review-v2" else "1",
        "workflow_id": state["workflow_id"], "design_id": state["design_id"],
        "evidence_id": state["evidence_id"], "run_id": record["run_id"],
        "execution": {"state": record["job_state"], "return_code": record["return_code"]},
        "target": campaign.target.model_dump(mode="json"),
        "parent": campaign.parent_pose.model_dump(mode="json"),
        "entity_state": "covalent_adduct", "representation": "mapped_product_with_dummy_sulfur",
        "policy": policy, "policy_sha256": sha256_file(policy_path),
        "calibration_status": "exploratory_default",
        "source_manifests": record["source_manifests"], "source_output_sha256": evaluation["output_sha256"],
        "evaluation_sha256": hashlib.sha256(json.dumps(evaluation, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest(),
        "implementation_sha256": {module.__name__: sha256_file(Path(module.__file__))
            for module in implementations},
        "report_implementation_sha256": sha256_file(Path(__file__)),
        "candidate_count": evaluation["candidate_count"],
        "posed_candidate_ids": input_binding["selected_candidate_ids"],
        "deferred_candidate_ids": input_binding["deferred_candidate_ids"],
        "exploratory_passing_candidate_count": evaluation["passing_candidate_count"],
        "incomplete_candidate_count": evaluation["incomplete_candidate_count"],
        "candidates": candidates, "limitations": LIMITATIONS,
        "physical_acceptance": "not_established", "validated_inhibitor": False,
        "md_ready": False, "compute_submitted_by_report": False,
    }
    target_binding = (state.get("refinement_input") or {}).get("target")
    if target_binding:
        from .ligand_refinement_target import load
        load(root, state)
        receipt = json.loads((root / target_binding["directory"] / "target-source.json").read_text())
        payload["refinement_target"] = {"artifact_id": target_binding["artifact_id"],
            "manifest_sha256": target_binding["manifest_sha256"],
            **{key: receipt[key] for key in ("status", "mapping", "projected_chain", "projected_site",
                "missing_construct_residue_count", "excluded_atom_count", "receptor_prepared",
                "af3_data_pipeline_run", "compute_submitted", "scientific_suitability_verified", "remaining")}}
    return payload


def render(payload):
    lines = ["# Ligand-first 探索性候选姿势报告", "",
        f"工作流：{payload['workflow_id']}；计算任务：{payload['run_id']}。",
        f"对接候选数：{payload['candidate_count']}；尚未对接候选数：{len(payload['deferred_candidate_ids'])}；"
        f"探索性规则通过数：{payload['exploratory_passing_candidate_count']}；"
        f"评价不完整数：{payload['incomplete_candidate_count']}。",
        "", "**未建立完整物理质量验收，也未验证共价抑制活性。**", ""]
    target = payload.get("refinement_target")
    if target:
        mapping = target["mapping"]
        lines += ["## 精修输入交接", "",
            f"已绑定研究来源：{mapping['entry_id']}，作者链 {mapping['auth_asym_id']} / Cys {mapping['author_residue_id']}，"
            f"对应 canonical C{mapping['uniprot_residue']}；投影链 {target['projected_chain']} / 序列位置 {target['projected_site']}。",
            f"构建体未解析残基 {target['missing_construct_residue_count']} 个；投影排除原子 {target['excluded_atom_count']} 个，逐项记录保存在准备清单中。",
            "这些是精修输入草案；受体准备、AF3 数据流水线和精修计算尚未执行，不能解释为 MD 准入或科学适用性通过。", ""]
    lines += ["## 生效策略与含义", "", "校准状态：exploratory_default。数值来自冻结计划。", ""]
    for definition in METRICS.values():
        lines.append(f"- {definition['metric_id']} / {definition['field']}：{definition['meaning']} "
                     f"当前判据 {definition['operator']} {payload['policy'][definition['policy_field']]} "
                     f"{definition['units']}。{definition['limitation']}")
    lines += ["", "## 候选与逐姿势证据", ""]
    for candidate in payload["candidates"]:
        lines += [f"### {candidate['candidate_id']}", "",
            f"游离分子 SMILES：`{candidate['free_ligand']['canonical_smiles']}`。",
            f"评价：{candidate['assessment']}；探索性规则标签：{candidate['exploratory_gate_status']}；"
            f"姿势数：{candidate['pose_count']}。", ""]
        if "independent_axes" in candidate:
            axes = candidate["independent_axes"]
            lines += [f"限定检查内的物理有效性：{axes['physical_validity']}；"
                f"设计约束：{axes['design_constraints']}；技术一致性：{axes['technical_qc']}；"
                f"证据完整：{axes['evidence_complete']}；可进入复核：{axes['review_eligible']}。",
                "各轴汇总表示是否存在满足该轴的姿势；联合准入要求同一姿势证据完整并满足全部约束，且无姿势评价错误；其他姿势的缺失仍单列报告。"
                "缺失不等于实测失败，PASS 不代表完整物理验收或抑制活性。", ""]
        for error in candidate["errors"]:
            lines.append("- 证据缺口：" + error)
        for pose in candidate["poses"]:
            lines += [f"- SDF 记录索引 {pose['source_pose_record_index']}："
                f"探索性规则 {pose['exploratory_gate_status']}；未满足项 {pose['exploratory_failure_reasons']}。",
                "```json", json.dumps(pose, indent=2, ensure_ascii=False, allow_nan=False), "```", ""]
    lines += ["## 结论边界", "", *["- " + item for item in LIMITATIONS], ""]
    return "\n".join(lines)


def freeze(root, state):
    payload = build(root, state)
    from .metric_contracts import md_not_evaluated
    payload["md_admission"] = md_not_evaluated()
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
    markdown = render(payload)
    identifier = "ligand-pose-report-" + hashlib.sha256(encoded.encode()).hexdigest()[:24]
    parent = root / "results/ligand-pose-reports"
    destination = parent / identifier
    for path in (root / "results", parent, destination):
        if path.is_symlink():
            raise ValueError("Pose report cannot traverse symbolic links")
    parent.mkdir(parents=True, exist_ok=True)
    previous = state.get("pose_report")
    if previous and previous["artifact_id"] == identifier:
        regular(root, destination / "manifest.json", previous["manifest_sha256"])
    if not destination.exists():
        staging = parent / ("." + identifier + "-" + uuid4().hex[:12])
        staging.mkdir()
        (staging / "report.json").write_text(encoded + "\n")
        (staging / "report.md").write_text(markdown)
        seal(staging)
        staging.rename(destination)
    for name in ("manifest.json", "report.json", "report.md"):
        regular(root, destination / name)
    verify(destination)
    if json.loads((destination / "report.json").read_text()) != payload:
        raise ValueError("Frozen pose report differs from current evidence")
    if (destination / "report.md").read_text() != markdown:
        raise ValueError("Frozen pose report text differs from current evidence")
    return {"artifact_id": identifier, "manifest_sha256": sha256_file(destination / "manifest.json"),
        "report_json": str((destination / "report.json").relative_to(root)),
        "report_markdown": str((destination / "report.md").relative_to(root)),
        "candidate_count": payload["candidate_count"], "physical_acceptance": "not_established",
        "validated_inhibitor": False, "md_ready": False, "compute_submitted_by_report": False}
