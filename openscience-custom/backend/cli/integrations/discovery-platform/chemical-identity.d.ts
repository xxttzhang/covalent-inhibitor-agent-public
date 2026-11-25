import { z } from "zod";
declare const identity: z.ZodObject<{
    state: z.ZodEnum<{
        invalid: "invalid";
        unavailable: "unavailable";
        unresolved: "unresolved";
        validated: "validated";
    }>;
    canonical: z.ZodNullable<z.ZodString>;
    reason: z.ZodString;
}, z.core.$strip>;
export type ChemicalIdentity = z.infer<typeof identity>;
export declare function chemicalIdentities(smiles: string[]): Promise<{
    version: string;
    identities: {
        state: "invalid" | "unavailable" | "unresolved" | "validated";
        canonical: string | null;
        reason: string;
    }[];
} | {
    version: null;
    identities: {
        state: "invalid" | "unavailable" | "unresolved" | "validated";
        canonical: string | null;
        reason: string;
    }[];
}>;
/** Group identity only; preserve every source, score, null, pose and status without ranking. */
export declare function candidatePool<T extends {
    recordID: string;
    candidate: {
        smiles: string;
    };
}>(records: T[]): Promise<{
    policy: string;
    rdkitVersion: string | null;
    ranked: boolean;
    sourceCount: number;
    groups: {
        id: string;
        identity: {
            state: "invalid" | "unavailable" | "unresolved" | "validated";
            canonical: string | null;
            reason: string;
        };
        sources: T[];
    }[];
}>;
export {};
