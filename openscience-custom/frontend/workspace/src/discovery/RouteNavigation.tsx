import { A, useSearchParams } from "@solidjs/router"
import { chatOrigin } from "./BackToChat"
import { For, Show } from "solid-js"

const routes = [
  { id: "studies", name: "统一研究", label: "计划与候选汇总" },
  { id: "library-first", name: "Library-first", label: "化合物库筛选" },
  { id: "ligand-first", name: "Ligand-first", label: "已知配体设计" },
  { id: "de-novo", name: "De novo", label: "从头生成" },
]
export default function RouteNavigation(props: { project?: string; active: string }) {
  const [params] = useSearchParams()
  const origin = () => chatOrigin(params.from) ? `?from=${encodeURIComponent(chatOrigin(params.from)!)}` : ""
  return <Show when={props.project}><nav class="discovery-routes" aria-label="研发路线"><For each={routes}>{(route, i) =>
    <A href={`/${props.project}/discovery/${route.id}${origin()}`} class={props.active === route.id ? "discovery-route-active" : "discovery-route-muted"}
      aria-current={props.active === route.id ? "page" : undefined}>
      {String(i() + 1).padStart(2, "0")} <b>{route.name}</b><small>{route.label}</small><span aria-hidden="true">↗</span>
    </A>}</For></nav><div class="discovery-version">Workbench v1.1.0</div></Show>
}
