import path from 'node:path'
import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'

export const scientificRoot = process.env.COVALENT_PIPELINE_ROOT
  ? path.resolve(process.env.COVALENT_PIPELINE_ROOT) : path.resolve(import.meta.dir, '../../../../../../science')
export const rankingScript = path.join(scientificRoot, 'scripts/rank_delivered_candidates.py')
export function rankingCode() {
  return createHash('sha256').update(readFileSync(rankingScript))
    .update(readFileSync(path.join(scientificRoot, 'src/covscreen/pareto.py'))).digest('hex')
}
