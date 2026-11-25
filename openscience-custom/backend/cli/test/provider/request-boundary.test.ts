import { expect, test } from "bun:test"
import { generateText } from "ai"
import { createOpenAICompatible } from "@ai-sdk/openai-compatible"
import { createProviderRequestBoundary, type ProviderRequestBoundary } from "../../src/provider/request-boundary"
import { Provider } from "../../src/provider/provider"
import { Instance } from "../../src/project/instance"
import { tmpdir } from "../fixture/fixture"

const boundary: ProviderRequestBoundary = {
  providerID: "bounded-fixture", modelID: "fixture-model", apiModelID: "fixture-model",
  endpointOrigin: "https://bounded.invalid", endpointBaseURL: "https://bounded.invalid/v1",
  contextTokenLimit: 100000, outputTokenLimit: 4096, maxOutputTokens: 128,
}
const endpoint = `${boundary.endpointBaseURL}/chat/completions`
const request = (body: Record<string, unknown> = {}) => ({ method: "POST", headers: { authorization: "Bearer TEST-SECRET" },
  body: JSON.stringify({ model: boundary.apiModelID, max_tokens: 128, messages: [{ role: "user", content: "TEST-PROMPT" }], ...body }) })

test("final send freezes the policy, disables redirects and permits exactly one completion", () => {
  const policy = { ...boundary }
  const guard = createProviderRequestBoundary(policy)
  policy.maxOutputTokens = 10000
  const input = request({ n: 1 })
  expect(guard(endpoint, input)).toEqual({ ...input, redirect: "error" })
  expect(() => guard(endpoint, input)).toThrow("cannot be retried")
  const frozen = { ...boundary }
  const second = createProviderRequestBoundary(frozen)
  frozen.maxOutputTokens = 10000
  expect(() => second(endpoint, request({ max_tokens: 129 }))).toThrow("output limit")
})

for (const url of ["https://other.invalid/v1/chat/completions", "https://bounded.invalid/v2/chat/completions",
  "https://bounded.invalid/v1/embeddings", "https://bounded.invalid/v1/chat/completions?api-version=x",
  "https://user:TEST-SECRET@bounded.invalid/v1/chat/completions", "http://bounded.invalid/v1/chat/completions"])
  test(`final URL rejects an unbound route: ${url.replace("TEST-SECRET", "redacted")}`, () => {
    expect(() => createProviderRequestBoundary(boundary)(url, request())).toThrow("endpoint differs")
  })

for (const body of [{ model: "other-model" }, { max_tokens: 129 }, { max_tokens: null }, { max_tokens: 0 },
  { max_tokens: 1.5 }, { max_tokens: undefined }, { max_output_tokens: 128 }, { n: 2 }, { n: "1" },
  { best_of: 2 }, { service_tier: "priority" }, { models: ["fixture-model", "fallback"] }, { num_return_sequences: 2 }])
  test(`final wire rejects model, count, routing or token changes: ${JSON.stringify(body)}`, () => {
    expect(() => createProviderRequestBoundary(boundary)(endpoint, request(body))).toThrow()
  })

test("both supported OpenAI token formats remain bounded and malformed bodies do not leak content", () => {
  for (const [suffix, field] of [["chat/completions", "max_completion_tokens"], ["responses", "max_output_tokens"]]) {
    const body = request({ max_tokens: undefined, [field!]: 100 })
    expect(createProviderRequestBoundary(boundary)(`${boundary.endpointBaseURL}/${suffix}`, body).redirect).toBe("error")
  }
  for (const body of ["TEST-SECRET invalid JSON TEST-PROMPT", "null", "[]"]) {
    try { createProviderRequestBoundary(boundary)(endpoint, { ...request(), body }); throw Error("expected rejection") }
    catch (error) {
      expect(String(error)).not.toContain("TEST-SECRET")
      expect(String(error)).not.toContain("TEST-PROMPT")
    }
  }
})

test("the real SDK serialization reaches the final guard and cannot reuse the bounded transport", async () => {
  const guard = createProviderRequestBoundary(boundary)
  const seen: { url: string; body: unknown; redirect?: RequestRedirect }[] = []
  const sdk = createOpenAICompatible({ name: "bounded-fixture", baseURL: boundary.endpointBaseURL, apiKey: "TEST-KEY",
    fetch: Object.assign(async (url: Parameters<typeof fetch>[0], options?: Parameters<typeof fetch>[1]) => {
      const checked = guard(url, options)
      seen.push({ url: String(url), body: JSON.parse(String(checked.body)), redirect: checked.redirect })
      // Synthetic transport fixture only. No external network/model invocation.
      return Response.json({ id: "synthetic-response", object: "chat.completion", created: 1, model: "fixture-model",
        choices: [{ index: 0, message: { role: "assistant", content: "synthetic fixture" }, finish_reason: "stop" }],
        usage: { prompt_tokens: 4, completion_tokens: 2, total_tokens: 6 } })
    }, { preconnect: () => {} }) })
  const model = sdk.languageModel("fixture-model")
  const result = await generateText({ model, prompt: "synthetic fixture", maxOutputTokens: 128, maxRetries: 0 })
  expect(result.text).toBe("synthetic fixture")
  expect(seen).toHaveLength(1)
  expect(seen[0]).toMatchObject({ url: endpoint, redirect: "error", body: { model: "fixture-model", max_tokens: 128 } })
  await expect(generateText({ model, prompt: "synthetic fixture", maxOutputTokens: 128, maxRetries: 0 })).rejects.toThrow("cannot be retried")
  expect(seen).toHaveLength(1)
})

test("provider cache and configuration changes cannot replace a bounded endpoint or contaminate ordinary calls", async () => {
  await using directory = await tmpdir({ config: { provider: {
    "bounded-fixture": { npm: "@ai-sdk/openai-compatible", options: { baseURL: boundary.endpointBaseURL, apiKey: "TEST-KEY" },
      models: { "fixture-model": { name: "Synthetic fixture", limit: { context: 100000, output: 4096 } } } },
  } } })
  await Instance.provide({ directory: directory.path, fn: async () => {
    const model = await Provider.getModel("bounded-fixture", "fixture-model")
    const ordinary = await Provider.getLanguage(model)
    const provider = await Provider.getProvider(model.providerID)
    // Simulate a configuration refresh after the host froze its price/endpoint.
    provider.options.baseURL = "https://changed.invalid/v1"
    const bounded = await Provider.getLanguage(model, boundary)
    expect(bounded).not.toBe(ordinary)
    await expect(generateText({ model: bounded, prompt: "synthetic", maxOutputTokens: 128, maxRetries: 0 }))
      .rejects.toThrow("endpoint differs")
    expect(await Provider.getLanguage(model)).toBe(ordinary)
    await expect(Provider.getLanguage({ ...model, limit: { ...model.limit, context: 200000 } }, boundary)).rejects.toThrow("identity or limits")
    provider.options.fetch = async () => { throw Error("custom transport must never be called") }
    await expect(Provider.getLanguage(model, boundary)).rejects.toBeInstanceOf(Provider.InitError)
  } })
})
