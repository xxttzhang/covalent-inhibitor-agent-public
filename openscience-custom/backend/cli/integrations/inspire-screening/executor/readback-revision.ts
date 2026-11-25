import { digest } from "./model"
import type { RefinementPlan } from "./refinement-approval"

// Explicit repair of an already executed, diagnosed run. Never migrates its
// scientific plan or extends compute approval. Other runs retain their reader.
export function readbackRevision(plan: RefinementPlan, root: string) {
  if (plan.id !== "refine-23316c6a85b77a1c57c59aac" ||
      digest(plan) !== "e0f3e0ee5ca6b30364790c2d828e50f914b88d8c9849a729c650471004cbff1a") return null
  return { version: "covdock-cached-coordinates-v1", plan_digest: digest(plan),
    original_sha256: "f2c46f599a3a2d35ef09087bbe7d4bc864182a3b2d9d4fef0beea6ffe31380ad",
    path: `${root}/inspect_covdock_cached_20260922.py`,
    sha256: "51d84756d5e296410b17d74d2739b9009940fb79f716c4c49b224e059d8c1b06" }
}
