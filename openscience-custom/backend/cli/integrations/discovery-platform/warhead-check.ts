import z from "zod"
import { warheadRegistry } from "@synsci/util/warhead-registry"
import { chemistrySandbox } from "./chemistry-sandbox"

const program = String.raw`
import json, sys
from rdkit import Chem, rdBase, RDLogger
RDLogger.DisableLog('rdApp.*')
data = json.load(sys.stdin)
patterns = [(p, Chem.MolFromSmarts(p['smarts'])) for p in data['patterns']]
if any(q is None for _,q in patterns): raise ValueError('Invalid registered pattern')
rows=[]
for text in data['smiles']:
    params=Chem.SmilesParserParams(); params.parseName=False; params.allowCXSMILES=False
    mol=Chem.MolFromSmiles(text,params)
    if mol is None or not mol.GetNumAtoms() or any(a.GetAtomicNum()==0 for a in mol.GetAtoms()):
        rows.append(dict(state='invalid',matches=[])); continue
    matches=[]
    for p,q in patterns:
        center=next(a.GetIdx() for a in q.GetAtoms() if a.GetAtomMapNum()==p['reactive_map'])
        seen=set()
        for match in mol.GetSubstructMatches(q):
            atom=match[center]
            if atom in seen: continue
            seen.add(atom)
            matches.append(dict(warhead=p['id'],electrophile_index=atom,matched_atoms=list(match)))
    rows.append(dict(state='recognized',matches=matches))
print(json.dumps(dict(rdkit_version=rdBase.rdkitVersion,rows=rows)))
`
const response = z.object({ rdkit_version: z.string(), rows: z.array(z.object({
  state: z.enum(["invalid", "recognized"]), matches: z.array(z.object({ warhead: z.string(),
    electrophile_index: z.number().int().nonnegative(), matched_atoms: z.array(z.number().int().nonnegative()) })) })) })

export async function checkWarheads(smiles: string[], sandboxed = true) {
  if (smiles.length > 10000 || smiles.some(text => text.length > 20000)) throw Error("Warhead check input exceeds limits")
  const isolated = sandboxed ? await chemistrySandbox(program) : null
  try {
    const child = Bun.spawn(isolated ? [isolated.planned.file, ...isolated.planned.args] : [process.env.OPENSCIENCE_RDKIT_PYTHON || "python3", "-c", program], {
      stdin: new Blob([JSON.stringify({ smiles, patterns: warheadRegistry.entries })]), stdout: "pipe", stderr: "ignore",
      ...(isolated ? { env: isolated.env, cwd: isolated.planned.temporary } : {}) })
    const timer = setTimeout(() => child.kill(), 15000)
    try {
      const [text, code] = await Promise.all([new Response(child.stdout).text(), child.exited])
      if (code !== 0) throw Error("RDKit warhead recognition unavailable")
      const result = response.parse(JSON.parse(text))
      if (result.rows.length !== smiles.length) throw Error("Warhead check count differs")
      return { ...result, registry: warheadRegistry.schema, atom_index_convention: "zero-based parsed input SMILES; not a pose atom mapping",
        establishes_reactivity: false, unknown_electrophiles_excluded: false }
    } finally { clearTimeout(timer) }
  } finally { isolated?.cleanup() }
}
