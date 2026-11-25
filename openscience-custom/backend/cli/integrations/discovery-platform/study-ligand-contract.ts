import { z } from "zod"
import type { DiscoveryStudy } from "@synsci/util/discovery-study"

/** Validate the real upstream compiler's binding, not just its READY label. */
export function studyLigandContract(study: DiscoveryStudy, original: string) {
  const schema = z.object({ hypothesis: z.object({ study_id: z.literal(study.id), route: z.literal("ligand-first"),
    parent_ligand: z.string(), parent_binding_mode: z.literal("noncovalent"), parent_evidence_ids: z.array(z.string()).min(1) }),
    campaign_registration: z.object({ historical_runs_reused: z.literal(false), input_binding: z.object({
      target: z.object({ symbol: z.string(), uniprot_id: z.string(), organism: z.string(),
        nucleophile: z.object({ residue_name: z.string(), uniprot_residue_number: z.number(), atom_name: z.string() }) }),
      parent_pose: z.object({ pdb_id: z.string(), ligand_name: z.string(), protein_chain_id: z.string(),
        target_author_residue_number: z.number(), target_uniprot_residue_number: z.number(), residue_number_offset: z.number() }),
      paths: z.object({ pdb: z.string(), reference_ligand: z.string(), campaign_path: z.string() }).passthrough(),
      project_id: z.string(), allowed_warheads: z.array(z.string()), seed_run_ids: z.record(z.string(), z.string()),
    }) }), handoff: z.object({ compute_submitted: z.literal(false), route_plan: z.object({ submitted: z.literal(false),
      chain: z.object({ next_stage: z.literal("structure_intake"), stages: z.array(z.object({
        state: z.literal("NOT_SUBMITTED"), run_id: z.null(), parent_run_id: z.null(),
      })).min(1) }),
      next_request: z.object({ project_id: z.string(), execution_mode: z.literal("plan"), human_approved: z.literal(false),
        parameters: z.object({ campaign: z.string() }).passthrough() }).passthrough(),
    }) }) })
  const result = schema.safeParse(JSON.parse(original))
  if (!result.success) return { state: "NEEDS_INPUTS_OR_ADAPTER", blockers: ["Missing validated Ligand-first input binding or fresh plan; no historical fallback"], compiled: false, submitted: false }
  const value = result.data
  const binding = value.campaign_registration.input_binding
  const target = study.target
  const route = study.routes["ligand-first"]
  const pose = binding.parent_pose
  const blockers: string[] = []
  if (!route.selected || route.mode !== "new") blockers.push("A new Ligand-first route must be selected")
  if (binding.target.symbol !== target.gene || binding.target.uniprot_id !== target.uniprot || target.taxon !== 9606
    || binding.target.organism !== "Homo sapiens" || binding.target.nucleophile.residue_name !== target.site.residue
    || binding.target.nucleophile.atom_name !== target.site.atom || pose.protein_chain_id !== target.site.chain
    || target.site.numbering !== "canonical" || binding.target.nucleophile.uniprot_residue_number !== target.site.number
    || pose.target_uniprot_residue_number !== target.site.number
    || pose.target_author_residue_number + pose.residue_number_offset !== target.site.number
    || "PDB:" + pose.pdb_id !== target.receptor.reference) blockers.push("Ligand-first study structure/site/chain/species differs from compiler inputs")
  if (route.inputs.parent?.toLowerCase() !== pose.ligand_name.toLowerCase()
    || value.hypothesis.parent_ligand.toLowerCase() !== pose.ligand_name.toLowerCase()
    || route.inputs.parentBinding !== "noncovalent") blockers.push("Explicit noncovalent parent differs from compiled campaign")
  if (route.inputs.parentStructure !== binding.paths.reference_ligand || route.inputs.receptorStructure !== binding.paths.pdb)
    blockers.push("Explicit parent/receptor input paths must match compiled campaign")
  if (binding.allowed_warheads.length !== 1 || binding.allowed_warheads[0] !== route.inputs.warhead)
    blockers.push("Study warhead differs from compiled campaign")
  const request = value.handoff.route_plan.next_request
  if (Object.keys(binding.seed_run_ids).length || request.project_id !== binding.project_id
    || request.parameters.campaign !== binding.paths.campaign_path) blockers.push("New plan identity or campaign input mismatch")
  // Snapshot comparison only: do not claim a fresh runtime/assets revalidation.
  return { state: blockers.length ? "NEEDS_INPUTS_OR_ADAPTER" : "UPSTREAM_PLAN_BINDING_VALIDATED", blockers,
    parameters: request, compiled: false, submitted: false }
}
