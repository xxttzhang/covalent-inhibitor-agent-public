import { digest } from "./model"

// Saved publication evidence is not a fresh download or a scientific verdict.
export function refinementPublicationStatus(
  report: ({ workflow_id: string } & Record<string, unknown>) | null,
  delivery: { workflow_id: string; state: string; report_digest: string }[],
) {
  const published = !!report && delivery.some((row) => row.workflow_id === report.workflow_id &&
    row.state === "COMPLETE" && row.report_digest === digest(report))
  return {
    platform_files_published: published,
    delivery_note: published
      ? "当前报告对应的原生 Results 交付记录已完成，可从平台文件入口查看和下载。该记录不替代实时下载核验或科学质量评估。"
      : "当前报告尚无匹配的已完成原生 Results 交付记录。remote_path 是远端文件，不是浏览器下载链接。",
    publication_evidence: "保存的原生文件交付记录；不等同于此刻所有文件均未被用户移动或删除。",
  }
}
