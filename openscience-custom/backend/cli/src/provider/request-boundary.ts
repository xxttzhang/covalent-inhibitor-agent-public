import z from "zod"

/** Optional host policy for one explicitly budgeted OpenAI-compatible request.
 * No credential or prompt data is included in boundary errors. */
export const providerRequestBoundary = z.object({
  providerID: z.string().min(1), modelID: z.string().min(1), apiModelID: z.string().min(1),
  endpointOrigin: z.string().url(), endpointBaseURL: z.string().url(),
  contextTokenLimit: z.number().int().positive(), outputTokenLimit: z.number().int().positive(),
  maxOutputTokens: z.number().int().positive(),
}).strict().superRefine((value, context) => {
  const url = new URL(value.endpointBaseURL)
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.origin !== value.endpointOrigin)
    context.addIssue({ code: "custom", message: "Request boundary requires an exact credential-free HTTPS base URL and origin" })
  if (value.maxOutputTokens > value.outputTokenLimit)
    context.addIssue({ code: "custom", message: "Request boundary exceeds the provider output limit" })
})
export type ProviderRequestBoundary = z.infer<typeof providerRequestBoundary>

/** A separate closure is created for each bounded model invocation. SDK cache
 * reuse and concurrent ordinary calls cannot inherit or replace this policy. */
export function createProviderRequestBoundary(input: ProviderRequestBoundary) {
  const boundary = providerRequestBoundary.parse(input)
  const base = new URL(boundary.endpointBaseURL).href.replace(/\/$/, "")
  const endpoints = new Set([`${base}/chat/completions`, `${base}/responses`])
  let attempted = false
  return (input: RequestInfo | URL, init?: RequestInit): RequestInit => {
    if (attempted) throw Error("Bounded provider request cannot be retried or produce another completion")
    attempted = true
    if (typeof input !== "string" && !(input instanceof URL))
      throw Error("Bounded provider request requires an explicit URL and serialized JSON body")
    const url = (() => { try { return new URL(input) } catch { throw Error("Final provider endpoint is not a valid URL") } })()
    if (url.origin !== boundary.endpointOrigin || !endpoints.has(url.href) || url.username || url.password || url.search || url.hash)
      throw Error("Final provider endpoint differs from the frozen request boundary")
    if (init?.method !== "POST" || typeof init.body !== "string")
      throw Error("Bounded provider request requires a single JSON POST")
    const decoded: unknown = (() => { try { return JSON.parse(init.body) } catch { throw Error("Bounded provider request body is not valid JSON") } })()
    const parsed = z.record(z.string(), z.unknown()).safeParse(decoded)
    if (!parsed.success) throw Error("Bounded provider request body is not a JSON object")
    const body = parsed.data
    if (body.model !== boundary.apiModelID)
      throw Error("Final provider model differs from the frozen request boundary")
    if ((body.n !== undefined && body.n !== 1) || (body.best_of !== undefined && body.best_of !== 1) ||
      ["models", "route", "provider", "service_tier", "serviceTier", "endpoint", "baseURL", "num_return_sequences",
        "num_completions", "sample_count", "requests", "batch"].some(key => body[key] !== undefined))
      throw Error("Bounded provider request forbids extra completions or unpriced routing options")
    const limits = ["max_tokens", "max_completion_tokens", "max_output_tokens"].filter(key => body[key] !== undefined)
    const maximum = limits.length === 1 ? body[limits[0]!] : undefined
    if (typeof maximum !== "number" || !Number.isSafeInteger(maximum) || maximum < 1 || maximum > boundary.maxOutputTokens)
      throw Error("Final provider output limit is missing, ambiguous or exceeds the frozen request boundary")
    // A redirect would move the body/credentials beyond the URL just verified.
    return { ...init, redirect: "error" }
  }
}
