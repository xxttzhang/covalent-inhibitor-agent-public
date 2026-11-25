import { createWorkflowProjection, declaredInterface, matchRouteIntent, type RecordTools, type WorkflowInterface } from "./route-workflow"
import { Storage } from "../storage/storage"

// Compatibility with 1.25 servers which lack the backend interface declaration.
// Current servers declare this contract on the entry tool's schema.
const fallback: WorkflowInterface = {
  schema: "research-workflow-interface-v1", route: "ligand-first",
  protocol: "ligand-workflow-v1", id_pattern: "^ligand-flow-[a-f0-9]{24}$",
  entry: "covalent_ligand_workflow",
  tools: ["covalent_ligand_workflow", "covalent_ligand_evidence", "covalent_ligand_design_step"],
  start_fields: ["action", "request_id", "target", "site", "taxon", "evidence_id", "workflow_id"],
  legacy_names: ["covalent_ligand_design", "covalent_plan_ligand_first", "covalent_evaluate_ligand_first_candidate"],
  legacy_prefixes: ["covalent_ligand_first_"],
  refreshable: { covalent_ligand_evidence: ["read", "read_sources", "read_parent_evidence", "supplement", "search_parent_literature", "measure_parent"] },
}
const storage = { key: "ligand-workflow-bindings", requestSuffix: "ligand" }
const compatible = createWorkflowProjection(fallback, storage)
export const workflowView = compatible.workflowView
export const bindLigandArguments = compatible.bindArguments

// Discovery intent does not select scientific identity or authorize execution.
export function ligandIntent(text: string) {
  return matchRouteIntent(text) === "ligand-first"
}

export function projectLigandTools(tools: RecordTools, selected?: string,
  view?: NonNullable<ReturnType<typeof workflowView>>) {
  return createWorkflowProjection(declaredInterface(tools, fallback, selected), storage).projectTools(tools, selected, view)
}

export async function ligandWorkflowTools(tools: RecordTools, scope: { project: string; session: string; intent: boolean }) {
  // A bound workflow keeps its own server contract even when another server is added.
  const saved = await Storage.read<{ entry: string }>([storage.key, scope.project, scope.session]).catch((error) => {
    if (error instanceof Storage.NotFoundError) return undefined
    throw error
  })
  return createWorkflowProjection(declaredInterface(tools, fallback, saved?.entry), storage).resolveTools(tools, scope)
}
