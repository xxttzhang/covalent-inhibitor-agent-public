import { Storage } from "../../src/storage/storage"
import { study, studyRequest } from "@synsci/util/discovery-study"
import { readStudy, createStudy } from "./study-store"
import { Lock } from "../../src/util/lock"
import { isDeepStrictEqual } from "node:util"
import z from "zod"

const binding = z.object({ directory: z.string(), project: z.string(), session: z.string(),
  study_id: study.shape.id }).strict()
type Scope = { directory: string; project: string; session: string }
const key = (scope: Scope) => ["study-session-binding", scope.project, scope.session]
const optional = (key: string[]) => Storage.read(key).catch(error => {
  if (error instanceof Storage.NotFoundError) return null
  throw error
})

export async function createSessionStudy(scope: Scope, input: unknown) {
  const request = studyRequest.parse(input)
  using lock = await Lock.write(key(scope).join(":"))
  const saved = await optional(key(scope))
  if (saved) {
    const id = await sessionStudy(scope)
    const plan = await readStudy(scope.directory, scope.project, id)
    const existing = { title: plan.title, target: plan.target, hypothesis: plan.hypothesis, routes: plan.routes,
      ...(plan.denovoBranches ? { denovoBranches: plan.denovoBranches } : {}) }
    if (!isDeepStrictEqual(request, existing))
      throw Error(`This session already has study ${id}; inspect it instead of creating a replacement. Use a separate research session for a different study.`)
    return { plan, reused: true }
  }
  const intent = ["study-session-creation", scope.project, scope.session]
  if (await optional(intent))
    throw Error("Study creation outcome is unknown; inspect existing studies without repeating create")
  await Storage.write(intent, { ...scope, request, started: new Date().toISOString() })
  const plan = await createStudy(scope.directory, scope.project, request)
  await bindStudySession(scope, plan.id)
  await Storage.remove(intent)
  return { plan, reused: false }
}

export async function bindStudySession(scope: Scope, id: string) {
  await readStudy(scope.directory, scope.project, id)
  await Storage.write(key(scope), binding.parse({ ...scope, study_id: id }))
}

/** Optional discovery read. Corrupt/cross-project bindings never become unbound. */
export async function boundSessionStudy(scope: Scope) {
  const saved = await optional(key(scope))
  if (!saved) return undefined
  const value = binding.parse(saved)
  if (value.directory !== scope.directory || value.project !== scope.project || value.session !== scope.session)
    throw Error("Study session binding scope mismatch")
  return readStudy(scope.directory, scope.project, value.study_id)
}

/** Resolve omission only. Never repair, guess or silently replace an explicit ID. */
export async function sessionStudy(scope: Scope, explicit?: string) {
  if (explicit !== undefined) {
    study.shape.id.parse(explicit)
    await readStudy(scope.directory, scope.project, explicit)
    return explicit
  }
  const saved = await Storage.read(key(scope)).catch(error => {
    if (error instanceof Storage.NotFoundError) return null
    throw error
  })
  if (!saved) throw Error("No study bound to this session; create a study or get an explicit ID from list")
  const value = binding.parse(saved)
  if (value.directory !== scope.directory || value.project !== scope.project || value.session !== scope.session)
    throw Error("Study session binding scope mismatch")
  await readStudy(scope.directory, scope.project, value.study_id)
  return value.study_id
}
