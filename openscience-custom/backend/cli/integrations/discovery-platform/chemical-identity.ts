import { z } from "zod"
import { createHash } from "node:crypto"
import { chemistrySandbox } from "./chemistry-sandbox"

// Fixed program: molecular inputs are JSON on stdin, never command-line code.
// Embedded so the hot-release bundle cannot lose a companion Python file.
const program = String.raw`
import json, sys
from rdkit import Chem, rdBase
from rdkit import RDLogger
RDLogger.DisableLog('rdApp.*')
def identity(text):
    params = Chem.SmilesParserParams()
    params.parseName = False
    params.allowCXSMILES = False
    mol = Chem.MolFromSmiles(text, params)
    if mol is None or mol.GetNumAtoms() == 0:
        return dict(state='invalid', canonical=None, reason='SMILES could not be validated')
    if any(atom.GetAtomicNum() == 0 for atom in mol.GetAtoms()):
        return dict(state='unresolved', canonical=None, reason='Wildcard atom: not a defined molecule')
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)
    canonical = Chem.MolToSmiles(mol, canonical=True, isomericSmiles=True)
    unknown = any(str(info.specified) != 'Specified' for info in Chem.FindPotentialStereo(mol))
    if unknown:
        return dict(state='unresolved', canonical=canonical, reason='Unspecified stereochemistry: kept separate')
    return dict(state='validated', canonical=canonical, reason='Exact isomeric graph; no salt, charge or tautomer normalization')
values = json.load(sys.stdin)
print(json.dumps(dict(version=rdBase.rdkitVersion, identities=[identity(text) for text in values])))
`

const identity = z.object({ state: z.enum(["validated", "invalid", "unresolved", "unavailable"]),
  canonical: z.string().nullable(), reason: z.string() })
const response = z.object({ version: z.string().min(1), identities: z.array(identity) })
export type ChemicalIdentity = z.infer<typeof identity>

export async function chemicalIdentities(smiles: string[], sandboxed = false) {
  if (!smiles.length) return { version: null, identities: [] as ChemicalIdentity[] }
  const unavailable = (reason: string) => ({ version: null,
    identities: smiles.map(() => ({ state: "unavailable" as const, canonical: null, reason })) })
  if (smiles.length > 10000 || smiles.some(value => value.length > 20000)) return unavailable("Identity batch exceeds validation limit")
  try {
    const isolated = sandboxed ? await chemistrySandbox(program) : null
    try {
    const child = Bun.spawn(isolated ? [isolated.planned.file, ...isolated.planned.args] : [process.env.OPENSCIENCE_RDKIT_PYTHON || "python3", "-c", program], {
      stdin: new Blob([JSON.stringify(smiles)]), stdout: "pipe", stderr: "ignore",
      ...(isolated ? { env: isolated.env, cwd: isolated.planned.temporary } : {}),
    })
    const timer = setTimeout(() => child.kill(), 15000)
    try {
      const [text, code] = await Promise.all([new Response(child.stdout).text(), child.exited])
      if (code !== 0) return unavailable("RDKit validation unavailable; no chemical merging performed")
      const parsed = response.parse(JSON.parse(text))
      if (parsed.identities.length !== smiles.length) throw Error("Identity count mismatch")
      return parsed
    } finally { clearTimeout(timer) }
    } finally { isolated?.cleanup() }
  } catch {
    return unavailable("RDKit validation unavailable; no chemical merging performed")
  }
}

/** Group identity only; preserve every source, score, null, pose and status without ranking. */
export async function candidatePool<T extends { recordID: string; candidate: { smiles: string } }>(records: T[], localChemistry = true) {
  const identities = await chemicalIdentities(records.map(record => record.candidate.smiles), !localChemistry)
  const groups = new Map<string, { id: string; identity: ChemicalIdentity; sources: T[] }>()
  records.forEach((record, index) => {
    const identity = identities.identities[index]
    const key = identity.state === "validated" ? "molecule:" + identity.canonical : "record:" + record.recordID
    const id = createHash("sha256").update(key).digest("hex")
    const existing = groups.get(id)
    if (existing) existing.sources.push(record)
    else groups.set(id, { id, identity, sources: [record] })
  })
  return { policy: "exact-isomeric-graph-v1", rdkitVersion: identities.version,
    ranked: false, sourceCount: records.length, groups: [...groups.values()] }
}
