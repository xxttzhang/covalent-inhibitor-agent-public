import type { DiscoveryStudy } from "@synsci/util/discovery-study"
import { targetParameters } from "@synsci/util/screening-target"
import { targetAdmission } from "../inspire-screening/executor/target"

/** Read-only admission against the installed Library-first executor, not a
 * second permissive target registry. A supported declaration is not readiness. */
export function studyLibraryAdmission(study: DiscoveryStudy) {
  const target = study.target
  const route = study.routes["library-first"]
  const parsed = targetParameters.safeParse({ target: target.gene,
    receptor: target.receptor.reference.replace(/^PDB:/i, ""), receptor_sha256: target.receptor.sha256,
    chain: target.site.chain, residue: target.site.residue.toUpperCase().replace(/^C$/, "CYS"),
    residue_number: target.site.number, numbering: target.site.numbering,
    insertion_code: "", reaction_type: "Michael Addition" })
  if (target.taxon !== 9606 || target.uniprot !== "P00533" || target.site.atom !== "SG"
    || route.inputs.warhead !== "acrylamide" || !parsed.success)
    return { supported: false, reason: "当前执行适配器未核验此物种、蛋白、位点或弹头组合；未提交，不替换输入。" }
  return targetAdmission(parsed.data)
}
