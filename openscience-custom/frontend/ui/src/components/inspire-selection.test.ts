import { afterAll, expect, test } from "bun:test"
import { fileURLToPath } from "node:url"
import { createServer } from "vite"
import solid from "vite-plugin-solid"
import { inspireSelection } from "./inspire-selection"

// Isolated in-memory SSR compilation. No app restart, listener or platform writes.
const server = await createServer({ configFile: false, root: fileURLToPath(new URL("../..", import.meta.url)),
  logLevel: "silent", plugins: [solid({ ssr: true, dev: false })], appType: "custom",
  server: { middlewareMode: true, hmr: false, watch: null } })
afterAll(() => server.close())
const subject = await server.ssrLoadModule("/src/components/inspire-selection-card.tsx") as typeof import("./inspire-selection-card")
const web = await server.ssrLoadModule("solid-js/web") as typeof import("solid-js/web")
const base = { version: 1, task_id: "screen-" + "a".repeat(24), plan_digest: "b".repeat(64),
  recorded_at: 1700000000000, grants_resource_approval: false, additional_budget_points: null,
  proposal_submitted_compute: false, state: "SUBSET_PROPOSED", selection_id: "c".repeat(64), selection_manifest_digest: "d".repeat(64),
  input_count: 8, eligible_count: 8, excluded_count: 0, selected_count: 2, effective_cap: 2,
  calibrated_uncertainty: false, note: "历史记录；排名分歧未校准。",
  policy: { max_candidates: 2, max_fraction: 0.25, disagreement_threshold: 0.5, top_fraction: 0.2,
    uncertainty_count: 1, consensus_count: 1, diversity_count: 0 },
  candidates: [{ input_sdf_index: 1, compound_id: "<script>unsafe()</script>", selection_channel: "rank_disagreement_review",
    carsidock_rank_fraction: 0, rtmscore_rank_fraction: 1, rank_disagreement: 1 },
    { input_sdf_index: 2, compound_id: "fixture-B", selection_channel: "consensus_sentinel",
      carsidock_rank_fraction: 0.1, rtmscore_rank_fraction: 0.1, rank_disagreement: 0 }] } satisfies NonNullable<ReturnType<typeof inspireSelection>>
const render = (value: unknown) => web.renderToString(() => subject.InspireSelectionCard({ value, task: base.task_id, digest: base.plan_digest }))

test("actual candidate component renders all reasons, unknown budget and escaped IDs without approval controls", () => {
  expect(inspireSelection(base, base.task_id, base.plan_digest)).toEqual(base)
  const html = render(base)
  expect(html).toContain("fixture-B")
  expect(html).toContain("排名分歧复核")
  expect(html).toContain("一致性对照")
  expect(html).toContain("未在提案中确定（不是 0 点券）")
  expect(html).toContain("本提案不授予精筛资源权限")
  expect(html).toContain("&lt;script>")
  expect(html).not.toContain("<script>")
  expect(html).not.toContain("<button")
  expect(html).toContain(base.selection_manifest_digest)
})

test("empty and invalid latest evidence show distinct messages, never an older candidate table", () => {
  const empty = render({ ...base, state: "NO_CANDIDATES_MEET_POLICY", selected_count: 0, candidates: [] })
  expect(empty).toContain("无候选符合策略")
  expect(empty).toContain("没有自动补满名额")
  expect(empty).not.toContain("<table")
  const invalid = render({ version: 1, task_id: base.task_id, plan_digest: base.plan_digest,
    recorded_at: base.recorded_at, grants_resource_approval: false, additional_budget_points: null,
    proposal_submitted_compute: false, state: "UNAVAILABLE", reason: "最新证据不一致" })
  expect(invalid).toContain("证据待检查")
  expect(invalid).toContain("最新证据不一致")
  expect(invalid).not.toContain("fixture-B")
})

test("wrong-task metadata, invented permission or invalid counts cannot render as a verified proposal", () => {
  for (const change of [{ task_id: "screen-" + "f".repeat(24) }, { plan_digest: "f".repeat(64) },
    { grants_resource_approval: true }, { additional_budget_points: 0 }, { proposal_submitted_compute: true },
    { selected_count: 100 }, { effective_cap: 3 }, { calibrated_uncertainty: true },
    { candidates: base.candidates.map((row) => ({ ...row, selection_channel: "__proto__" })) }]) {
    expect(render({ ...base, ...change })).not.toContain("已保存的精筛候选提案")
  }
})

test("bounded full candidate lists are rendered without a silent preview truncation", () => {
  const candidates = Array.from({ length: 100 }, (_, index) => ({ ...base.candidates[0], input_sdf_index: index, compound_id: `candidate-${index}` }))
  const html = render({ ...base, input_count: 400, eligible_count: 400, selected_count: 100, effective_cap: 100,
    policy: { ...base.policy, max_candidates: 100, uncertainty_count: 100, consensus_count: 0 }, candidates })
  expect(html).toContain("candidate-99")
  expect((html.match(/<tr(?:\s[^>]*)?>/g) ?? []).length).toBe(101)
})
