import type { Hono, Context } from "hono"
import { studyActionResult } from "./study-actions"
import { isAllowedOrigin } from "../../src/server/host-guard"

type Scope = { directory: string; project: string; authorize: () => Promise<void>; localChemistry?: () => Promise<boolean>; authorizeRemote?: () => Promise<void> }
type Resolve = () => Scope
// v2's installed closure passed only four arguments to execute. A new router
// generation is required to forward remote authorization without restarting.
const key = Symbol.for("openscience.discovery.study-http.v3")
const registrations: WeakMap<Hono, { resolve: Resolve; execute: typeof studyActionResult }> = Reflect.get(globalThis, key) ?? new WeakMap()
Reflect.set(globalThis, key, registrations)

/** Replace only the unmatched endpoint's terminal handler, preserving the host's
 * request guard, authentication and project Instance middleware. Works after Hono
 * router compilation and requires no server restart. */
export function installStudyHTTP(app: Hono, resolve: Resolve) {
  const existing = registrations.get(app)
  if (existing) { existing.resolve = resolve; existing.execute = studyActionResult; return }
  const registration = { resolve, execute: studyActionResult }
  registrations.set(app, registration)
  const match = app.router.match.bind(app.router)
  app.router.match = (method, pathname) => {
    const found = match(method, pathname)
    if (pathname !== "/api/discovery/study") return found
    const handlers = found[0].map((entry, index) => {
      if (index !== found[0].length - 1) return entry
      const handler = async (context: Context) => {
        context.header("Cache-Control", "no-store")
        if (method !== "POST") return context.json({ error: "Use POST with an explicit study action" }, 405)
        if (!context.req.header("content-type")?.toLowerCase().startsWith("application/json")) return context.json({ error: "JSON body required" }, 415)
        // The local UI and API use different ports. Retain the host's origin
        // policy instead of rejecting its already-trusted frontend origin.
        const origin = context.req.header("origin")
        if (origin && origin !== new URL(context.req.url).origin && !isAllowedOrigin(origin))
          return context.json({ error: "Cross-origin study action denied" }, 403)
        if (Number(context.req.header("content-length") ?? 0) > 1048576) return context.json({ error: "Study request too large" }, 413)
        const scope = registration.resolve()
        await scope.authorize()
        const body = await context.req.text()
        if (body.length > 1048576) return context.json({ error: "Study request too large" }, 413)
        try {
          return context.json(await registration.execute(scope.directory, scope.project, JSON.parse(body), await scope.localChemistry?.() ?? false, scope.authorizeRemote))
        } catch (error) {
          return context.json({ error: error instanceof Error ? error.message : "Study action failed" }, 400)
        }
      }
      return [[handler, entry[0][1]], entry[1]] as typeof entry
    })
    return [handlers, found[1]] as typeof found
  }
}
