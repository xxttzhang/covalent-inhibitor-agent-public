import { z } from "zod"

const reference = z.object({ path: z.string().min(1),
  sha256: z.string().regex(/^[a-f0-9]{64}$/) }).strict()
const atom = z.object({ role: z.string().min(1), index: z.number().int().nonnegative() }).strict()
const residue = z.object({ chain: z.string(), number: z.number().int(),
  insertionCode: z.string(), residue: z.string().min(1), atom: z.string().min(1) }).strict()

/** Evidence reported by a producer, not an assertion that the consumer verified it. */
export const candidateEvidence = z.object({
  schema: z.literal("candidate-scientific-evidence-v1"),
  candidateID: z.string().min(1),
  smiles: z.string().min(1),
  pose: z.object({
    state: z.literal("reported"), representation: z.enum(["prereaction", "adduct"]),
    molecule: reference, recordIndex: z.number().int().nonnegative(),
    receptor: reference.optional(), method: z.string().min(1), evidence: reference,
    coordinateBinding: z.literal("not_independently_verified"),
  }).strict().optional(),
  reactionAtoms: z.object({
    state: z.literal("reported"), warhead: z.string().min(1),
    representation: z.enum(["prereaction", "adduct"]),
    molecule: reference, recordIndex: z.number().int().nonnegative(),
    indexConvention: z.enum(["zero_based_sdf", "zero_based_rdkit_removeHs"]),
    atoms: z.array(atom).min(1), method: z.string().min(1), evidence: reference,
  }).strict().refine(value => new Set(value.atoms.map(item => item.role)).size === value.atoms.length
    && new Set(value.atoms.map(item => item.index)).size === value.atoms.length,
  "Reaction atom roles and indices must be unique").optional(),
  targetMapping: z.object({
    state: z.literal("reported"), uniprot: z.string().min(1), taxon: z.number().int().positive(),
    canonicalResidue: z.number().int().positive(), structure: residue,
    receptor: reference, method: z.string().min(1), evidence: reference,
  }).strict().optional(),
  measurements: z.array(z.object({
    metricID: z.string().min(1), scope: z.enum(["candidate", "md"]),
    stage: z.enum(["chemical", "prereaction", "adduct", "md"]),
    method: z.string().min(1), definition: z.string().min(1), unit: z.string().min(1),
    inputs: z.array(reference).min(1), evidence: reference,
  }).strict()),
}).strict()
