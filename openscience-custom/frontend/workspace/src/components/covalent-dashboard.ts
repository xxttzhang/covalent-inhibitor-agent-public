export type CovalentStageState = "completed" | "running" | "queued" | "pending" | "failed"

export interface CovalentPipelineStage {
  id: string
  label: string
  status: CovalentStageState
  count?: number
  input_count?: number
  success_rate?: number
  run_id?: string
  updated_at?: string
  quality_gates?: Record<string, boolean>
}

export interface CovalentLibraryFirstStatus {
  run_id: string
  state: "completed" | "running" | "queued" | "failed"
  target: string
  source: string
  processed: number
  selected: number
  progress: number
  quality?: Record<string, unknown>
  quality_gates: Record<string, boolean>
  stages: CovalentPipelineStage[]
  campaign_id?: string
  next_stage?: string
  chain_terminal_state?: string
}

export interface CovalentTask {
  id: string
  name: string
  route: "library-first" | "ligand-first" | "de-novo"
  status: CovalentStageState | "ready"
  progress: number
  detail?: string
  updated_at?: string
  run_id?: string
}

export interface CovalentDashboard {
  schema_version: string
  generated_at?: string
  tasks?: CovalentTask[]
  library_first?: CovalentLibraryFirstStatus
}

const pipelineStates = new Set(["completed", "running", "queued", "failed"])
const stageStates = new Set([...pipelineStates, "pending"])
const taskStates = new Set([...stageStates, "ready"])

function parseTasks(value: unknown): CovalentTask[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is CovalentTask => {
    if (!item || typeof item !== "object") return false
    const task = item as Record<string, unknown>
    return (
      typeof task.id === "string" &&
      typeof task.name === "string" &&
      (task.route === "library-first" || task.route === "ligand-first" || task.route === "de-novo") &&
      typeof task.status === "string" &&
      taskStates.has(task.status) &&
      typeof task.progress === "number" &&
      (task.detail === undefined || typeof task.detail === "string") &&
      (task.updated_at === undefined || typeof task.updated_at === "string") &&
      (task.run_id === undefined || typeof task.run_id === "string")
    )
  })
}

export function parseCovalentDashboard(value: unknown): CovalentDashboard | undefined {
  if (!value || typeof value !== "object") return
  const payload = value as Record<string, unknown>
  if (typeof payload.schema_version !== "string") return
  const tasks = parseTasks(payload.tasks)
  const library = payload.library_first
  if (library === undefined)
    return {
      schema_version: payload.schema_version,
      generated_at: typeof payload.generated_at === "string" ? payload.generated_at : undefined,
      tasks,
    }
  if (!library || typeof library !== "object") return
  const current = library as Record<string, unknown>
  if (
    typeof current.run_id !== "string" ||
    typeof current.state !== "string" ||
    !pipelineStates.has(current.state) ||
    typeof current.target !== "string" ||
    typeof current.source !== "string" ||
    typeof current.processed !== "number" ||
    typeof current.selected !== "number" ||
    typeof current.progress !== "number" ||
    !Array.isArray(current.stages) ||
    !current.quality_gates ||
    typeof current.quality_gates !== "object"
  )
    return
  const stages = current.stages.filter((item): item is CovalentPipelineStage => {
    if (!item || typeof item !== "object") return false
    const stage = item as Record<string, unknown>
    return (
      typeof stage.id === "string" &&
      typeof stage.label === "string" &&
      typeof stage.status === "string" &&
      stageStates.has(stage.status) &&
      (stage.count === undefined || typeof stage.count === "number") &&
      (stage.input_count === undefined || typeof stage.input_count === "number") &&
      (stage.success_rate === undefined || typeof stage.success_rate === "number") &&
      (stage.run_id === undefined || typeof stage.run_id === "string") &&
      (stage.updated_at === undefined || typeof stage.updated_at === "string") &&
      (stage.quality_gates === undefined ||
        (!!stage.quality_gates && typeof stage.quality_gates === "object"))
    )
  })
  if (stages.length !== current.stages.length) return
  return {
    schema_version: payload.schema_version,
    generated_at: typeof payload.generated_at === "string" ? payload.generated_at : undefined,
    tasks,
    library_first: {
      run_id: current.run_id,
      state: current.state as CovalentLibraryFirstStatus["state"],
      target: current.target,
      source: current.source,
      processed: current.processed,
      selected: current.selected,
      progress: Math.max(0, Math.min(100, current.progress)),
      quality: current.quality && typeof current.quality === "object" ? (current.quality as Record<string, unknown>) : {},
      quality_gates: current.quality_gates as Record<string, boolean>,
      stages,
      campaign_id: typeof current.campaign_id === "string" ? current.campaign_id : undefined,
      next_stage: typeof current.next_stage === "string" ? current.next_stage : undefined,
      chain_terminal_state:
        typeof current.chain_terminal_state === "string" ? current.chain_terminal_state : undefined,
    },
  }
}
