import { z } from "zod"
import { study, studyRequest, studyRoutes } from "@synsci/util/discovery-study"
import { createStudy, importHistorical, listStudies, readStudy, studyOverview, registeredStudySource } from "./study-store"
import { attachHypothesis, readHypothesis, readHypotheses, listHypotheses, syncHypotheses } from "./study-hypothesis"
import { readReasoning, startReasoning, reasoningStatus, resumeReasoning } from "./study-reasoning"
import { compileLiveLibrary, readLibraryPlan } from "./study-live-plan"
import { compileDeNovoPlan, readDeNovoPlan, registeredGenerationPackage } from "./study-denovo-plan"
import { readStudyGenerationProfile, assertGenerationBinding } from "./study-denovo-compiler"
import { bindLigandPlan, readLigandPlan } from "./study-ligand-plan"
import { studyLibraryAdmission } from "./study-admission"
import { listReusableHypotheses, readHypothesisReuses, reuseHypothesis } from "./study-hypothesis-reuse"

export const studyAction = z.discriminatedUnion("action", [
  z.object({ action: z.literal("list") }).strict(),
  z.object({ action: z.literal("create"), request: studyRequest }).strict(),
  z.object({ action: z.literal("get"), study_id: study.shape.id }).strict(),
  z.object({ action: z.literal("sync-hypotheses"), study_id: study.shape.id }).strict(),
  z.object({ action: z.literal("reuse-hypothesis"), study_id: study.shape.id, source_study: study.shape.id,
    route: z.enum(studyRoutes), sha256: z.string().regex(/^[a-f0-9]{64}$/) }).strict(),
  z.object({ action: z.literal("import"), study_id: study.shape.id, route: z.enum(studyRoutes) }).strict(),
  z.object({ action: z.literal("attach-hypothesis"), study_id: study.shape.id, source: z.string() }).strict(),
  z.object({ action: z.literal("start-reasoning"), study_id: study.shape.id }).strict(),
  z.object({ action: z.literal("reasoning-status"), study_id: study.shape.id }).strict(),
  z.object({ action: z.literal("resume-reasoning"), study_id: study.shape.id }).strict(),
  z.object({ action: z.literal("compile-library"), study_id: study.shape.id }).strict(),
  z.object({ action: z.literal("compile-denovo"), study_id: study.shape.id }).strict(),
  z.object({ action: z.literal("bind-ligand-plan"), study_id: study.shape.id }).strict(),
])

/** Shared by the native agent tool and UI; callers must apply project authorization. */
export async function studyActionResult(directory: string, project: string, input: unknown, localChemistry = false,
  authorizeRemote?: () => Promise<void>, parentSession?: string) {
  const args = studyAction.parse(input)
  if (args.action === "list") return { studies: await listStudies(directory, project), registeredSource: await registeredStudySource(directory, project) }
  if (args.action === "create") {
    const plan = await createStudy(directory, project, args.request)
    return { study_id: plan.id, plan, newComputeSubmitted: false,
      next: plan.routes["de-novo"].selected && plan.routes["de-novo"].mode === "new"
        ? { tool: "denovo_generate", action: "workflow", study_id: plan.id }
        : "Import selected historical routes, or validate new-mode inputs with their execution adapters." }
  }
  if (args.action === "import") return importHistorical(directory, project, args.study_id, args.route)
  if (args.action === "sync-hypotheses") return syncHypotheses(directory, await readStudy(directory, project, args.study_id))
  if (args.action === "reuse-hypothesis") return reuseHypothesis(directory, await readStudy(directory, project, args.study_id), args.source_study, args.route, args.sha256)
  if (args.action === "attach-hypothesis") return attachHypothesis(directory, await readStudy(directory, project, args.study_id), args.source)
  if (args.action === "start-reasoning") return startReasoning(directory, await readStudy(directory, project, args.study_id), parentSession)
  if (args.action === "reasoning-status") return reasoningStatus(directory, await readStudy(directory, project, args.study_id))
  if (args.action === "resume-reasoning") return resumeReasoning(directory, await readStudy(directory, project, args.study_id), parentSession)
  if (args.action === "compile-denovo") return compileDeNovoPlan(directory, await readStudy(directory, project, args.study_id))
  if (args.action === "bind-ligand-plan") return bindLigandPlan(directory, await readStudy(directory, project, args.study_id))
  if (args.action === "compile-library") {
    if (!authorizeRemote) throw Error("Host remote-read authorization is required")
    return compileLiveLibrary(directory, await readStudy(directory, project, args.study_id), authorizeRemote)
  }
  const overview = await studyOverview(directory, project, args.study_id, localChemistry)
  const libraryPlan = await readLibraryPlan(directory, overview.plan)
  const denovoPlan = await readDeNovoPlan(directory, overview.plan)
  const ligandPlan = await readLigandPlan(directory, overview.plan)
  const routes = await Promise.all(overview.routes.map(async route => {
    if (route.imported || overview.plan.routes[route.route].mode !== "new") return route
    if (route.route === "library-first" && libraryPlan) return { ...route, state: "plan-compiled", missing: [],
      reason: "已编译正式计划，未批准、未提交；执行前仍需资源复核和原生审批" }
    if (route.route === "de-novo" && denovoPlan) return { ...route, state: "local-plan-compiled", missing: [],
      reason: "已编译本地注册包计划，未批准、未提交；远端资产与实时资源待验证" }
    if (route.route === "ligand-first" && ligandPlan) return { ...route, state: "upstream-plan-bound", missing: [],
      reason: "已校验并接入上游编译快照，未批准、未提交；远端执行条件仍待复核" }
    if (route.route === "library-first" && route.state === "awaiting-adapter-validation") {
      const admission = studyLibraryAdmission(overview.plan)
      if (!admission.supported) return { ...route, state: "unsupported", reason: admission.reason }
    }
    if (route.route === "de-novo" && route.state === "awaiting-adapter-validation") {
      const profile = await readStudyGenerationProfile(await registeredGenerationPackage())
      try { assertGenerationBinding(overview.plan, profile) }
      catch (error) { return { ...route, state: "unsupported", reason: String(error) } }
    }
    return route
  }))
  return { ...overview, hypothesisAttachment: await readHypothesis(directory, overview.plan),
    routes, libraryPlan, denovoPlan, ligandPlan,
    reasoning: await readReasoning(directory, overview.plan),
    hypothesisReuses: await readHypothesisReuses(directory, overview.plan),
    reusableHypotheses: await listReusableHypotheses(directory, overview.plan),
    hypothesisAttachments: await readHypotheses(directory, overview.plan),
    hypothesisSources: await listHypotheses(directory, overview.plan) }
}
