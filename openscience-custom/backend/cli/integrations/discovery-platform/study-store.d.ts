import { studyRoutes } from "@synsci/util/discovery-study";
export declare function createStudy(directory: string, project: string, input: unknown): Promise<{
    title: string;
    target: {
        id: string;
        gene: string;
        taxon: number;
        uniprot: string;
        site: {
            chain: string;
            residue: string;
            number: number;
            atom: string;
            numbering: "canonical" | "structure";
        };
        receptor: {
            reference: string;
            sha256?: string | undefined;
        };
    };
    hypothesis: {
        state: "pending" | "provided" | "reused";
        rationale: string;
        evidence: {
            reference: string;
            sha256?: string | undefined;
        }[];
    };
    routes: Record<"de-novo" | "library-first" | "ligand-first", {
        selected: boolean;
        mode: "new" | "reuse";
        inputs: {
            library?: string | undefined;
            parent?: string | undefined;
            parentBinding?: "covalent" | "noncovalent" | "unknown" | undefined;
            pocket?: string | undefined;
            warhead?: string | undefined;
        };
        reason?: string | undefined;
    }>;
    schema: "discovery-study-v1";
    id: string;
    project: string;
    createdAt: string;
}>;
export declare function readStudy(directory: string, project: string, id: string): Promise<{
    title: string;
    target: {
        id: string;
        gene: string;
        taxon: number;
        uniprot: string;
        site: {
            chain: string;
            residue: string;
            number: number;
            atom: string;
            numbering: "canonical" | "structure";
        };
        receptor: {
            reference: string;
            sha256?: string | undefined;
        };
    };
    hypothesis: {
        state: "pending" | "provided" | "reused";
        rationale: string;
        evidence: {
            reference: string;
            sha256?: string | undefined;
        }[];
    };
    routes: Record<"de-novo" | "library-first" | "ligand-first", {
        selected: boolean;
        mode: "new" | "reuse";
        inputs: {
            library?: string | undefined;
            parent?: string | undefined;
            parentBinding?: "covalent" | "noncovalent" | "unknown" | undefined;
            pocket?: string | undefined;
            warhead?: string | undefined;
        };
        reason?: string | undefined;
    }>;
    schema: "discovery-study-v1";
    id: string;
    project: string;
    createdAt: string;
}>;
export declare function listStudies(directory: string, project: string): Promise<{
    title: string;
    target: {
        id: string;
        gene: string;
        taxon: number;
        uniprot: string;
        site: {
            chain: string;
            residue: string;
            number: number;
            atom: string;
            numbering: "canonical" | "structure";
        };
        receptor: {
            reference: string;
            sha256?: string | undefined;
        };
    };
    hypothesis: {
        state: "pending" | "provided" | "reused";
        rationale: string;
        evidence: {
            reference: string;
            sha256?: string | undefined;
        }[];
    };
    routes: Record<"de-novo" | "library-first" | "ligand-first", {
        selected: boolean;
        mode: "new" | "reuse";
        inputs: {
            library?: string | undefined;
            parent?: string | undefined;
            parentBinding?: "covalent" | "noncovalent" | "unknown" | undefined;
            pocket?: string | undefined;
            warhead?: string | undefined;
        };
        reason?: string | undefined;
    }>;
    schema: "discovery-study-v1";
    id: string;
    project: string;
    createdAt: string;
}[]>;
/** Only registered current-project route outputs can enter this ingress. No shell or compute. */
export declare function importHistorical(directory: string, project: string, id: string, route: typeof studyRoutes[number]): Promise<{
    schema: "discovery-study-import-v1";
    study: string;
    project: string;
    target: string;
    route: "de-novo" | "library-first" | "ligand-first";
    importedAt: string;
    mode: "historical-reuse";
    source: {
        path: string;
        sha256: string;
        project: string;
        run: string;
        evidenceUpdatedAt: string;
    };
    snapshot: string;
    candidates: number;
    newComputeSubmitted: false;
}>;
export declare function readImport(directory: string, project: string, id: string, route: typeof studyRoutes[number]): Promise<{
    receipt: {
        schema: "discovery-study-import-v1";
        study: string;
        project: string;
        target: string;
        route: "de-novo" | "library-first" | "ligand-first";
        importedAt: string;
        mode: "historical-reuse";
        source: {
            path: string;
            sha256: string;
            project: string;
            run: string;
            evidenceUpdatedAt: string;
        };
        snapshot: string;
        candidates: number;
        newComputeSubmitted: false;
    };
    data: {
        schema: "discovery-result-v1";
        project: string;
        target: string;
        route: "de-novo" | "library-first" | "ligand-first";
        run: string;
        collectedAt: string;
        evidenceUpdatedAt: string;
        observation: "local-evidence" | "scheduler";
        current: string;
        notice: string;
        stages: {
            id: string;
            label: string;
            state: "blocked" | "completed" | "failed" | "pending" | "running" | "skipped";
            status: string;
            count: string;
            title: string;
            detail: string;
            facts: string[];
            progress?: {
                completed: number;
                total: number;
                unit: string;
            } | undefined;
        }[];
        stats: {
            label: string;
            value: string;
            unit: string;
            note: string;
        }[];
        candidates: {
            id: string;
            label: string;
            smiles: string;
            featured: boolean;
            badge: string;
            tone: "amber" | "blue" | "gray" | "teal";
            note: string;
            metrics: {
                id: string;
                label: string;
                value: number | null;
                unit: string;
                definition: string;
                uncertainty?: {
                    kind: "CI95" | "SD" | "SEM";
                    value: number;
                    independentN: number;
                } | undefined;
            }[];
            structures: {
                method: string;
                format: "cif" | "mae" | "maegz" | "pdb" | "sdf";
                asset: {
                    storage: "project";
                    path: string;
                    sha256: string;
                    mediaType: string;
                    name: string;
                } | {
                    storage: "artifact";
                    id: string;
                    version: string;
                    sha256: string;
                    mediaType: string;
                    name: string;
                };
                pose?: number | undefined;
                identity: Record<string, string>;
            }[];
            md: {
                state: "failed" | "pending" | "ready" | "running";
                summary: string;
                independentReplicas: number;
                metrics: {
                    id: string;
                    label: string;
                    value: number | null;
                    unit: string;
                    definition: string;
                    uncertainty?: {
                        kind: "CI95" | "SD" | "SEM";
                        value: number;
                        independentN: number;
                    } | undefined;
                }[];
                chartIds: string[];
            };
        }[];
        analysis: {
            state: "analysis" | "failed" | "ready" | "rendering" | "report" | "validation" | "waiting";
            expectedReplicas: number;
            observedReplicas: number;
            message: string;
        };
        charts: {
            id: string;
            candidate: string;
            kind: string;
            title: string;
            caption: string;
            asset: {
                storage: "project";
                path: string;
                sha256: string;
                mediaType: string;
                name: string;
            } | {
                storage: "artifact";
                id: string;
                version: string;
                sha256: string;
                mediaType: string;
                name: string;
            };
            xUnit: string;
            yUnit: string;
        }[];
        reports: {
            title: string;
            asset: {
                storage: "project";
                path: string;
                sha256: string;
                mediaType: string;
                name: string;
            } | {
                storage: "artifact";
                id: string;
                version: string;
                sha256: string;
                mediaType: string;
                name: string;
            };
        }[];
        sources: {
            path: string;
            sha256: string;
            updated: string;
        }[];
        legacy?: {
            kind: "denovo" | "refinement";
            data: unknown;
        } | undefined;
    };
}>;
export declare function studyOverview(directory: string, project: string, id: string): Promise<{
    schema: string;
    plan: {
        title: string;
        target: {
            id: string;
            gene: string;
            taxon: number;
            uniprot: string;
            site: {
                chain: string;
                residue: string;
                number: number;
                atom: string;
                numbering: "canonical" | "structure";
            };
            receptor: {
                reference: string;
                sha256?: string | undefined;
            };
        };
        hypothesis: {
            state: "pending" | "provided" | "reused";
            rationale: string;
            evidence: {
                reference: string;
                sha256?: string | undefined;
            }[];
        };
        routes: Record<"de-novo" | "library-first" | "ligand-first", {
            selected: boolean;
            mode: "new" | "reuse";
            inputs: {
                library?: string | undefined;
                parent?: string | undefined;
                parentBinding?: "covalent" | "noncovalent" | "unknown" | undefined;
                pocket?: string | undefined;
                warhead?: string | undefined;
            };
            reason?: string | undefined;
        }>;
        schema: "discovery-study-v1";
        id: string;
        project: string;
        createdAt: string;
    };
    routes: ({
        state: "not-selected";
        missing: never[];
        reason: string;
        route: "de-novo" | "library-first" | "ligand-first";
        imported: null;
    } | {
        state: "awaiting-import";
        missing: never[];
        reason: string;
        route: "de-novo" | "library-first" | "ligand-first";
        imported: null;
    } | {
        state: "awaiting-adapter-validation" | "missing-inputs";
        missing: string[];
        reason: string;
        route: "de-novo" | "library-first" | "ligand-first";
        imported: null;
    } | {
        route: "de-novo" | "library-first" | "ligand-first";
        state: "historical-imported";
        missing: never[];
        reason: string;
        imported: {
            receipt: {
                schema: "discovery-study-import-v1";
                study: string;
                project: string;
                target: string;
                route: "de-novo" | "library-first" | "ligand-first";
                importedAt: string;
                mode: "historical-reuse";
                source: {
                    path: string;
                    sha256: string;
                    project: string;
                    run: string;
                    evidenceUpdatedAt: string;
                };
                snapshot: string;
                candidates: number;
                newComputeSubmitted: false;
            };
            data: {
                schema: "discovery-result-v1";
                project: string;
                target: string;
                route: "de-novo" | "library-first" | "ligand-first";
                run: string;
                collectedAt: string;
                evidenceUpdatedAt: string;
                observation: "local-evidence" | "scheduler";
                current: string;
                notice: string;
                stages: {
                    id: string;
                    label: string;
                    state: "blocked" | "completed" | "failed" | "pending" | "running" | "skipped";
                    status: string;
                    count: string;
                    title: string;
                    detail: string;
                    facts: string[];
                    progress?: {
                        completed: number;
                        total: number;
                        unit: string;
                    } | undefined;
                }[];
                stats: {
                    label: string;
                    value: string;
                    unit: string;
                    note: string;
                }[];
                candidates: {
                    id: string;
                    label: string;
                    smiles: string;
                    featured: boolean;
                    badge: string;
                    tone: "amber" | "blue" | "gray" | "teal";
                    note: string;
                    metrics: {
                        id: string;
                        label: string;
                        value: number | null;
                        unit: string;
                        definition: string;
                        uncertainty?: {
                            kind: "CI95" | "SD" | "SEM";
                            value: number;
                            independentN: number;
                        } | undefined;
                    }[];
                    structures: {
                        method: string;
                        format: "cif" | "mae" | "maegz" | "pdb" | "sdf";
                        asset: {
                            storage: "project";
                            path: string;
                            sha256: string;
                            mediaType: string;
                            name: string;
                        } | {
                            storage: "artifact";
                            id: string;
                            version: string;
                            sha256: string;
                            mediaType: string;
                            name: string;
                        };
                        pose?: number | undefined;
                        identity: Record<string, string>;
                    }[];
                    md: {
                        state: "failed" | "pending" | "ready" | "running";
                        summary: string;
                        independentReplicas: number;
                        metrics: {
                            id: string;
                            label: string;
                            value: number | null;
                            unit: string;
                            definition: string;
                            uncertainty?: {
                                kind: "CI95" | "SD" | "SEM";
                                value: number;
                                independentN: number;
                            } | undefined;
                        }[];
                        chartIds: string[];
                    };
                }[];
                analysis: {
                    state: "analysis" | "failed" | "ready" | "rendering" | "report" | "validation" | "waiting";
                    expectedReplicas: number;
                    observedReplicas: number;
                    message: string;
                };
                charts: {
                    id: string;
                    candidate: string;
                    kind: string;
                    title: string;
                    caption: string;
                    asset: {
                        storage: "project";
                        path: string;
                        sha256: string;
                        mediaType: string;
                        name: string;
                    } | {
                        storage: "artifact";
                        id: string;
                        version: string;
                        sha256: string;
                        mediaType: string;
                        name: string;
                    };
                    xUnit: string;
                    yUnit: string;
                }[];
                reports: {
                    title: string;
                    asset: {
                        storage: "project";
                        path: string;
                        sha256: string;
                        mediaType: string;
                        name: string;
                    } | {
                        storage: "artifact";
                        id: string;
                        version: string;
                        sha256: string;
                        mediaType: string;
                        name: string;
                    };
                }[];
                sources: {
                    path: string;
                    sha256: string;
                    updated: string;
                }[];
                legacy?: {
                    kind: "denovo" | "refinement";
                    data: unknown;
                } | undefined;
            };
        };
    })[];
    newComputeSubmitted: boolean;
    candidates: {
        recordID: string;
        study: string;
        route: "de-novo" | "library-first" | "ligand-first";
        sourceRun: string;
        sourceSha256: string;
        mode: string;
        candidate: {
            id: string;
            label: string;
            smiles: string;
            featured: boolean;
            badge: string;
            tone: "amber" | "blue" | "gray" | "teal";
            note: string;
            metrics: {
                id: string;
                label: string;
                value: number | null;
                unit: string;
                definition: string;
                uncertainty?: {
                    kind: "CI95" | "SD" | "SEM";
                    value: number;
                    independentN: number;
                } | undefined;
            }[];
            structures: {
                method: string;
                format: "cif" | "mae" | "maegz" | "pdb" | "sdf";
                asset: {
                    storage: "project";
                    path: string;
                    sha256: string;
                    mediaType: string;
                    name: string;
                } | {
                    storage: "artifact";
                    id: string;
                    version: string;
                    sha256: string;
                    mediaType: string;
                    name: string;
                };
                pose?: number | undefined;
                identity: Record<string, string>;
            }[];
            md: {
                state: "failed" | "pending" | "ready" | "running";
                summary: string;
                independentReplicas: number;
                metrics: {
                    id: string;
                    label: string;
                    value: number | null;
                    unit: string;
                    definition: string;
                    uncertainty?: {
                        kind: "CI95" | "SD" | "SEM";
                        value: number;
                        independentN: number;
                    } | undefined;
                }[];
                chartIds: string[];
            };
        };
    }[];
    pool: {
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
            sources: {
                recordID: string;
                study: string;
                route: "de-novo" | "library-first" | "ligand-first";
                sourceRun: string;
                sourceSha256: string;
                mode: string;
                candidate: {
                    id: string;
                    label: string;
                    smiles: string;
                    featured: boolean;
                    badge: string;
                    tone: "amber" | "blue" | "gray" | "teal";
                    note: string;
                    metrics: {
                        id: string;
                        label: string;
                        value: number | null;
                        unit: string;
                        definition: string;
                        uncertainty?: {
                            kind: "CI95" | "SD" | "SEM";
                            value: number;
                            independentN: number;
                        } | undefined;
                    }[];
                    structures: {
                        method: string;
                        format: "cif" | "mae" | "maegz" | "pdb" | "sdf";
                        asset: {
                            storage: "project";
                            path: string;
                            sha256: string;
                            mediaType: string;
                            name: string;
                        } | {
                            storage: "artifact";
                            id: string;
                            version: string;
                            sha256: string;
                            mediaType: string;
                            name: string;
                        };
                        pose?: number | undefined;
                        identity: Record<string, string>;
                    }[];
                    md: {
                        state: "failed" | "pending" | "ready" | "running";
                        summary: string;
                        independentReplicas: number;
                        metrics: {
                            id: string;
                            label: string;
                            value: number | null;
                            unit: string;
                            definition: string;
                            uncertainty?: {
                                kind: "CI95" | "SD" | "SEM";
                                value: number;
                                independentN: number;
                            } | undefined;
                        }[];
                        chartIds: string[];
                    };
                };
            }[];
        }[];
    };
}>;
