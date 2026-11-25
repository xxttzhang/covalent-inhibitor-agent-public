import { A, useSearchParams } from "@solidjs/router"

export function chatOrigin(value: unknown) {
  return typeof value === "string" && /^(new|ses_[A-Za-z0-9]+)$/.test(value) ? value : undefined
}

export default function BackToChat(props: { project?: string; session?: string }) {
  const [params] = useSearchParams()
  return <A class="discovery-back" href={`/${props.project}/session/${chatOrigin(params.from) ?? props.session ?? "new"}`}>← 返回聊天</A>
}
