import z from "zod"

// Scientific intent, not a resource grant or an inferred sequence mapping.
export const targetParameters = z.object({
  target: z.string().trim().regex(/^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/),
  receptor: z.string().trim().min(1).max(1024).regex(/^[^\x00-\x1f]+$/),
  receptor_sha256: z.string().regex(/^[a-f0-9]{64}$/).optional(),
  chain: z.string().max(16).regex(/^[A-Za-z0-9_-]*$/),
  residue: z.string().regex(/^[A-Z0-9]{3}$/),
  residue_number: z.number().int().min(-99999).max(999999),
  insertion_code: z.string().regex(/^[A-Za-z0-9]?$/),
  numbering: z.enum(["canonical", "structure"]),
  reaction_type: z.string().trim().min(1).max(80).regex(/^[A-Za-z0-9 +()_-]+$/),
}).strict()
export type TargetParameters = z.infer<typeof targetParameters>

export const targetContext = z.object({
  task_id: z.string(), plan_digest: z.string().regex(/^[a-f0-9]{64}$/),
  artifact_sha256: z.string().regex(/^[a-f0-9]{64}$/),
  target: z.string(), site: z.string(), parameters: targetParameters.nullable(),
}).strict()
export type TargetContext = z.infer<typeof targetContext>

export function targetSite(value: TargetParameters) {
  const letters: Record<string, string> = { CYS: "C", SER: "S", LYS: "K", TYR: "Y", THR: "T", HIS: "H", ASP: "D", GLU: "E" }
  return `${letters[value.residue] ?? value.residue}${value.residue_number}${value.insertion_code}`
}

// A separate line is intentionally machine-readable. Never search quoted prose
// or a model's suggested arguments for evidence of user approval.
export function parseTargetParameters(text: string) {
  const lines = text.split("\n").filter((line) => line.startsWith("target_parameters="))
  if (!lines.length) return
  if (lines.length !== 1) throw new Error("只允许一组 target_parameters；请明确本次靶点。")
  return targetParameters.parse(JSON.parse(lines[0].slice("target_parameters=".length)))
}

export function targetCommand(value: TargetParameters, mode: string, points: number) {
  const parameters = targetParameters.parse(value)
  if (!["economy", "standard", "deep", "test100", "research_pilot_v1"].includes(mode) || !Number.isFinite(points) || points < 0)
    throw new Error("请选择筛选模式并填写非负点券上限。")
  return `启动筛选；CovInDB 虚拟筛选库；screening_mode=${mode}；预算${points}点券\ntarget_parameters=${JSON.stringify(parameters)}`
}
