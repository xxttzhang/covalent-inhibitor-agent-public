/** Conservative Chinese/English route intent. Returns a route only when the user's own
 * words identify exactly one; anything ambiguous returns undefined so the model asks.
 *
 * Order of evidence:
 *  1. Quoted text and code are ignored (a quoted route name is a mention, not a request).
 *  2. A question about a route term ("什么是 ligand-first") is not a request to start it.
 *  3. Explicit route names win; two different explicit names are ambiguous.
 *  4. Only without any explicit name, route-specific phrases are used, and they must
 *     name the route's actual subject (existing lead / compound library / de novo
 *     scaffold). Broad words such as "共价筛选" or "lead optimization" never bind. */
export type RouteName = "ligand-first" | "library-first" | "de-novo"
const routes: RouteName[] = ["ligand-first", "library-first", "de-novo"]

const explicit: Record<RouteName, RegExp> = {
  "ligand-first": /\bligand[\s_-]*first\b/i,
  "library-first": /\blibrary[\s_-]*first\b/i,
  "de-novo": /\bde[\s_-]*novo\b|\bpocketxmol\b/i,
}

const lead = String.raw`(?:先导|母体|已有|已知|现有|我的|我有)[^，。,.；;]{0,14}(?:配体|化合物|分子|抑制剂|先导物)`
const library = String.raw`(?:化合物库|分子库|筛选库|共价库|文库|covindb)`
const fuzzy: Record<RouteName, RegExp[]> = {
  "ligand-first": [
    new RegExp(lead + String.raw`.{0,16}(?:共价化|加(?:上)?.{0,6}弹头|引入.{0,6}弹头|改造成?共价|衍生(?:化)?成?共价|共价衍生)`, "i"),
    /基于.{0,6}(?:先导|母体|已有|已知)(?:配体|化合物|分子).{0,8}(?:共价化|改造|衍生|设计)/,
    /共价化改造/,
    /\bcovalent(?:ize|ise|ization|isation)\b.{0,40}\b(?:lead|ligand|inhibitor|hit)\b/i,
    /\b(?:lead|ligand|inhibitor|hit)\b.{0,40}\bcovalent(?:ize|ise|ization|isation)\b/i,
    /\b(?:convert|turn|transform|modify)\b.{0,40}\b(?:known|existing|reversible|non-?covalent)\b.{0,30}\b(?:ligand|inhibitor|lead|hit)\b.{0,40}\bcovalent/i,
    /\b(?:add|install|introduce|attach)\b.{0,20}\b(?:warhead|acrylamide)\b.{0,40}\b(?:to|onto)\b.{0,20}\b(?:my|the|this|a|known|existing)\b.{0,20}\b(?:ligand|inhibitor|lead)\b/i,
  ],
  "library-first": [
    new RegExp(library + String.raw`[^。；;]{0,16}(?:筛选|筛药|筛查|挑选)`, "i"),
    new RegExp(String.raw`(?:筛选|虚拟筛选|筛药)[^。；;]{0,16}` + library, "i"),
    /\b(?:screen(?:ing)?|virtual\s+screen(?:ing)?)\b.{0,40}\b(?:library|libraries|covindb|compound\s+database)\b/i,
    /\b(?:library|libraries|covindb)\b.{0,40}\bscreen(?:ing)?\b/i,
  ],
  "de-novo": [
    /从头(?:生成|设计|构建)/,
    /全新(?:的)?(?:骨架|分子|化合物|结构)?(?:的)?(?:生成|设计)/,
    /生成(?:全新|新颖)(?:的)?(?:骨架|分子|化合物)/,
    /\b(?:novel|new)\s+scaffolds?\b.{0,40}\b(?:generat|design)/i,
    /\bgenerat\w*\b.{0,30}\b(?:novel|new)\s+(?:scaffolds?|molecules?|compounds?)\b/i,
    /\bpocket[\s_-]*(?:based|conditioned)\b.{0,30}\b(?:generation|design|generative)\b/i,
  ],
}

const term = String.raw`(?:ligand[\s_-]*first|library[\s_-]*first|de[\s_-]*novo|从头(?:生成|设计)?|pocketxmol)`
const meta = new RegExp(
  String.raw`(?:什么是|何为|是什么|介绍(?:一下)?|解释(?:一下)?|讲讲|说说|what\s+is|what's|explain|describe|tell\s+me\s+about)\s*[^，。,.?？]{0,6}` + term +
  "|" + term + String.raw`\s*(?:路线|流程|方案)?\s*(?:是什么|是指|指的是|什么意思|怎么用|如何使用|有什么用|是干嘛的|怎么样|怎样|如何|好不好|靠谱吗|值得吗|适合吗|有什么优缺点)`, "i")

function withoutQuoted(text: string) {
  return text
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`[^`]*`/g, " ")
    .replace(/“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"/g, " ")
}

export function matchRouteIntent(text: string): RouteName | undefined {
  const clean = withoutQuoted(text)
  if (meta.test(clean)) return undefined
  const named = routes.filter((route) => explicit[route].test(clean))
  if (named.length) return named.length === 1 ? named[0] : undefined
  const inferred = routes.filter((route) => fuzzy[route].some((pattern) => pattern.test(clean)))
  return inferred.length === 1 ? inferred[0] : undefined
}
