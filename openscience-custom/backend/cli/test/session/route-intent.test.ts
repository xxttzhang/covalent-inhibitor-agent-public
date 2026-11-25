import { expect, test } from "bun:test"
import { matchRouteIntent, type RouteName } from "../../src/session/route-intent"
import { ligandIntent } from "../../src/session/ligand-workflow"
import { libraryIntent } from "../../src/session/library-workflow"
import { denovoIntent } from "../../src/session/denovo-entry"

// [utterance, expected route]. undefined means: never auto-bind, the model must ask.
const positives: [string, RouteName][] = [
  // Ligand-first: an existing lead is being covalentized
  ["帮我针对 EGFR C797 基于已有先导分子做共价化改造", "ligand-first"],
  ["我有一个非共价的 EGFR 抑制剂，想给它加上丙烯酰胺弹头做共价化", "ligand-first"],
  ["ligand-first 路线，针对 BTK C481", "ligand-first"],
  ["针对 EFGR，设计共价抑制剂，ligand-first 路线", "ligand-first"],
  ["Please covalentize my reversible BTK inhibitor lead", "ligand-first"],
  ["Convert my known noncovalent ligand into a covalent inhibitor targeting Cys797", "ligand-first"],
  ["把已知配体改造成共价抑制剂，靶点 KRAS G12C", "ligand-first"],
  ["Please perform lead covalentization for BTK", "ligand-first"],
  // Library-first: screen a compound library
  ["从共价化合物库中筛选 EGFR C797 的抑制剂", "library-first"],
  ["用 library-first 做 BTK 的筛选", "library-first"],
  ["Run virtual screening on CovInDB for EGFR C797", "library-first"],
  ["Screen a covalent compound library against KRAS G12C", "library-first"],
  ["在 CovInDB 里面跑一次针对 JAK3 的筛选", "library-first"],
  ["对共价分子库做虚拟筛选，靶点 EGFR", "library-first"],
  // De novo: generate new scaffolds
  ["针对 EGFR C797 从头设计共价抑制剂", "de-novo"],
  ["全新骨架设计一个 BTK 共价抑制剂", "de-novo"],
  ["de novo 生成 EGFR 候选分子", "de-novo"],
  ["Generate novel scaffolds de novo for EGFR C797 with an acrylamide warhead", "de-novo"],
  ["Pocket-based covalent generation for KRAS G12C", "de-novo"],
  ["用 PocketXMol 从头生成 EGFR C797 丙烯酰胺候选", "de-novo"],
  ["针对 EGFR C797 全新骨架设计共价抑制剂", "de-novo"],
  // An explicit route name outranks incidental wording from another route
  ["用 ligand-first 路线，从化合物库里挑几个先导分子", "ligand-first"],
  ["帮我针对 EGFR 做 ligand-first，什么是最佳母体？", "ligand-first"],
]

const negatives: [string, string][] = [
  // No route named: ask, never guess
  ["设计共价抑制剂", "no route"],
  ["帮我针对 EGFR C797 设计一个共价抑制剂", "no route"],
  ["Design a covalent inhibitor for BTK", "no route"],
  ["我想做虚拟筛选", "bare virtual screening"],
  ["共价筛选的原理是什么", "bare 共价筛选 must not bind"],
  ["我们的筛选结果里有 3 个候选，帮我看看", "screening results, no library"],
  ["lead optimization meeting notes summary", "generic lead optimization"],
  ["帮我整理一下这批分子的结果", "unrelated"],
  // Questions about a route are not requests to start it
  ["什么是 ligand-first？", "question"],
  ["What is de novo design in this platform?", "question"],
  ["解释一下 library-first 是什么", "question"],
  ["什么是从头设计", "question"],
  ["ligand first 路线怎么样", "opinion question"],
  ["library-first 靠谱吗？", "opinion question"],
  ["做一个全新设计的共价库筛选", "two routes implied"],
  // Comparison and mixed intent
  ["比较 ligand-first 与 library-first 的区别", "comparison"],
  ["Compare library-first and de novo for EGFR", "comparison"],
  ["请总结一下 de novo 与 ligand-first 各自的优缺点", "comparison"],
  ["先从化合物库筛选，再基于先导分子共价化改造", "mixed fuzzy"],
  ["先用 ligand-first 改造，再用 de novo 生成补充", "mixed explicit"],
  // A quoted route name is a mention, not a request
  ["同事说“请用 ligand-first 路线”，你觉得靠谱吗？", "quoted"],
  ["The note says `de novo` was used last time, what do you think?", "code span"],
]

test("positive utterances bind exactly their route", () => {
  for (const [text, route] of positives) expect({ text, got: matchRouteIntent(text) }).toEqual({ text, got: route })
})

test("ambiguous, questions, comparisons and quotes never bind", () => {
  for (const [text, why] of negatives) expect({ text, why, got: matchRouteIntent(text) }).toEqual({ text, why, got: undefined })
})

test("per-route predicates agree with the classifier and are mutually exclusive", () => {
  for (const [text, route] of [...positives, ...negatives.map(([text]) => [text, undefined] as const)]) {
    const hits = [ligandIntent(text) && "ligand-first", libraryIntent(text) && "library-first", denovoIntent(text) && "de-novo"].filter(Boolean)
    expect({ text, hits }).toEqual({ text, hits: route ? [route] : [] })
  }
})

test("corpus covers the three routes, questions, comparisons and the unnamed-route boundary", () => {
  expect(positives.length + negatives.length).toBeGreaterThanOrEqual(40)
  for (const route of ["ligand-first", "library-first", "de-novo"]) expect(positives.filter(([, r]) => r === route).length).toBeGreaterThanOrEqual(6)
})
