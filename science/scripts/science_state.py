from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"


DDL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    name TEXT NOT NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    source_uri TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    evidence_type TEXT NOT NULL,
    citation TEXT NOT NULL,
    locator TEXT,
    sha256 TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relations (
    id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL REFERENCES entities(id),
    predicate TEXT NOT NULL,
    object_id TEXT NOT NULL REFERENCES entities(id),
    evidence_id TEXT REFERENCES evidence(id),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    attributes_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hypotheses (
    id TEXT PRIMARY KEY,
    statement TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('proposed', 'testing', 'supported', 'contradicted', 'retired')
    ),
    confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    assumptions_json TEXT NOT NULL DEFAULT '[]',
    disconfirming_evidence_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hypothesis_evidence (
    hypothesis_id TEXT NOT NULL REFERENCES hypotheses(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    stance TEXT NOT NULL CHECK (stance IN ('supports', 'contradicts', 'context')),
    PRIMARY KEY (hypothesis_id, evidence_id, stance)
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    producer TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS benchmark_runs (
    id TEXT PRIMARY KEY,
    benchmark_id TEXT NOT NULL,
    freeze_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    status TEXT NOT NULL,
    metrics_path TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (freeze_id, engine)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]
    return f"{prefix}:{digest}"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript(DDL)
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
        ("schema_version", SCHEMA_VERSION),
    )
    connection.commit()
    return connection


def upsert_entity(
    connection: sqlite3.Connection,
    entity_id: str,
    entity_type: str,
    name: str,
    attributes: dict[str, Any],
    source_uri: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO entities(id, entity_type, name, attributes_json, source_uri, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            entity_type=excluded.entity_type,
            name=excluded.name,
            attributes_json=excluded.attributes_json,
            source_uri=excluded.source_uri
        """,
        (entity_id, entity_type, name, json_text(attributes), source_uri, now()),
    )


def upsert_evidence(
    connection: sqlite3.Connection,
    evidence_id: str,
    evidence_type: str,
    citation: str,
    locator: str | None,
    digest: str | None,
    metadata: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO evidence(id, evidence_type, citation, locator, sha256, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            evidence_type=excluded.evidence_type,
            citation=excluded.citation,
            locator=excluded.locator,
            sha256=excluded.sha256,
            metadata_json=excluded.metadata_json
        """,
        (evidence_id, evidence_type, citation, locator, digest, json_text(metadata), now()),
    )


def upsert_relation(
    connection: sqlite3.Connection,
    subject_id: str,
    predicate: str,
    object_id: str,
    evidence_id: str | None,
    confidence: float,
    attributes: dict[str, Any] | None = None,
) -> None:
    relation_id = stable_id("rel", subject_id, predicate, object_id, evidence_id or "")
    connection.execute(
        """
        INSERT INTO relations(
            id, subject_id, predicate, object_id, evidence_id, confidence,
            attributes_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            evidence_id=excluded.evidence_id,
            confidence=excluded.confidence,
            attributes_json=excluded.attributes_json
        """,
        (
            relation_id,
            subject_id,
            predicate,
            object_id,
            evidence_id,
            confidence,
            json_text(attributes or {}),
            now(),
        ),
    )


def init_database(db_path: Path) -> dict[str, Any]:
    with connect(db_path):
        pass
    return {"database": str(db_path), "schema_version": SCHEMA_VERSION, "status": "PASS"}


def ingest_btk(db_path: Path, project_root: Path, manifest_path: Path) -> dict[str, Any]:
    root = project_root.resolve()
    manifest_abs = manifest_path if manifest_path.is_absolute() else root / manifest_path
    manifest = json.loads(manifest_abs.read_text())
    if manifest.get("frozen") is not True or manifest.get("status") != "PASS":
        raise ValueError("Only a passing frozen manifest can be ingested")

    manifest_rel = str(manifest_abs.resolve().relative_to(root))
    spec_rel = manifest["spec"]["path"]
    spec = json.loads((root / spec_rel).read_text())
    target = spec["target"]
    benchmark_id = f"benchmark:{spec['benchmark_id']}:{spec['version']}"
    target_id = f"target:{target['symbol']}"
    residue_id = f"residue:{target['symbol']}:{target['chain']}:{target['reactive_residue']}"
    freeze_entity_id = f"freeze:{manifest['freeze_id']}"
    created = now()

    with connect(db_path) as connection:
        upsert_entity(
            connection,
            target_id,
            "protein_target",
            target["name"],
            {"symbol": target["symbol"], "pdb_id": target["pdb_id"]},
            f"https://www.rcsb.org/structure/{target['pdb_id']}",
        )
        upsert_entity(
            connection,
            residue_id,
            "reactive_residue",
            f"{target['symbol']} {target['chain']} {target['reactive_residue']}",
            {
                "chain": target["chain"],
                "residue": target["reactive_residue"],
                "atom": target["reactive_atom"],
                "reaction": target["reaction"],
            },
        )
        upsert_entity(
            connection,
            benchmark_id,
            "benchmark",
            f"{spec['benchmark_id']} v{spec['version']}",
            {
                "library_size": spec["library"]["expected_rows"],
                "failure_policy": spec["failure_policy"],
            },
        )
        upsert_entity(
            connection,
            freeze_entity_id,
            "benchmark_freeze",
            manifest["freeze_id"],
            {"manifest_path": manifest_rel, "frozen": True},
        )

        manifest_hash = sha256_file(manifest_abs)
        manifest_evidence_id = stable_id("evidence", manifest_rel, manifest_hash)
        upsert_evidence(
            connection,
            manifest_evidence_id,
            "frozen_manifest",
            manifest["freeze_id"],
            manifest_rel,
            manifest_hash,
            {"benchmark_id": manifest["benchmark_id"]},
        )
        upsert_relation(connection, residue_id, "belongs_to", target_id, manifest_evidence_id, 1.0)
        upsert_relation(connection, benchmark_id, "evaluates", target_id, manifest_evidence_id, 1.0)
        upsert_relation(
            connection, freeze_entity_id, "freezes", benchmark_id, manifest_evidence_id, 1.0
        )

        artifact_map = {item["path"]: item for item in manifest["artifacts"]}
        for item in manifest["artifacts"]:
            artifact_id = stable_id("artifact", item["path"], item["sha256"])
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, artifact_type, path, sha256, producer, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    id=excluded.id,
                    artifact_type=excluded.artifact_type,
                    sha256=excluded.sha256,
                    producer=excluded.producer,
                    metadata_json=excluded.metadata_json
                """,
                (
                    artifact_id,
                    item["role"],
                    item["path"],
                    item["sha256"],
                    "BTK-mini-v1 freeze",
                    json_text({"size_bytes": item["size_bytes"], "freeze_id": manifest["freeze_id"]}),
                    created,
                ),
            )
            evidence_id = stable_id("evidence", item["path"], item["sha256"])
            upsert_evidence(
                connection,
                evidence_id,
                item["role"],
                item["path"],
                item["path"],
                item["sha256"],
                {"freeze_id": manifest["freeze_id"]},
            )

        for engine in spec["engines"]:
            engine_id = f"engine:{engine['id']}"
            upsert_entity(
                connection,
                engine_id,
                "screening_engine",
                engine["id"],
                {"score_direction": engine["score_direction"]},
            )
            metrics_path = engine["metrics_path"]
            metrics = json.loads((root / metrics_path).read_text())
            metric_artifact = artifact_map[metrics_path]
            metric_evidence_id = stable_id(
                "evidence", metrics_path, metric_artifact["sha256"]
            )
            upsert_relation(
                connection,
                engine_id,
                "evaluated_on",
                benchmark_id,
                metric_evidence_id,
                1.0,
                {
                    "n_input": metrics["n_input"],
                    "n_success": metrics["n_scoring_success"],
                    "n_failed": metrics["n_failed"],
                },
            )
            run_id = stable_id("run", manifest["freeze_id"], engine["id"])
            connection.execute(
                """
                INSERT INTO benchmark_runs(
                    id, benchmark_id, freeze_id, engine, status, metrics_path,
                    manifest_path, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(freeze_id, engine) DO UPDATE SET
                    status=excluded.status,
                    metrics_path=excluded.metrics_path,
                    manifest_path=excluded.manifest_path,
                    metadata_json=excluded.metadata_json
                """,
                (
                    run_id,
                    spec["benchmark_id"],
                    manifest["freeze_id"],
                    engine["id"],
                    "FROZEN",
                    metrics_path,
                    manifest_rel,
                    json_text(
                        {
                            "score_direction": metrics["score_direction"],
                            "n_input": metrics["n_input"],
                            "n_success": metrics["n_scoring_success"],
                            "n_failed": metrics["n_failed"],
                            "conservative": metrics["conservative"],
                        }
                    ),
                    created,
                ),
            )
        connection.commit()

    return {
        "database": str(db_path),
        "manifest": manifest_rel,
        "freeze_id": manifest["freeze_id"],
        "engines_ingested": len(spec["engines"]),
        "artifacts_ingested": len(manifest["artifacts"]),
        "status": "PASS",
    }


def ingest_project(db_path: Path, repository_root: Path, project_dir: Path) -> dict[str, Any]:
    """Ingest a target-independent project dossier into the scientific state graph."""
    root = repository_root.resolve()
    absolute_project = project_dir if project_dir.is_absolute() else root / project_dir
    absolute_project = absolute_project.resolve()
    absolute_project.relative_to(root)
    project = json.loads((absolute_project / "project.json").read_text())
    passport = json.loads((absolute_project / project["target_passport"]).read_text())
    assessment = json.loads(
        (absolute_project / "assessment" / "covalent_assessment.json").read_text()
    )
    hypothesis = json.loads(
        (absolute_project / "hypotheses" / "hypothesis-001.json").read_text()
    )
    route = json.loads((absolute_project / "plans" / "route_plan.json").read_text())
    project_id = f"project:{project['project_id']}"
    target_id = f"target:{passport['target_id']}"
    site = passport["site"]
    site_id = f"site:{project['project_id']}:{site['site_id']}"

    with connect(db_path) as connection:
        upsert_entity(
            connection,
            project_id,
            "research_project",
            project["title"],
            {
                "objective": project["objective"],
                "status": project["status"],
                "route": route["selected_route"],
                "decision": assessment["decision"],
            },
        )
        upsert_entity(
            connection,
            target_id,
            "protein_target",
            passport["name"],
            {
                "symbol": passport["symbol"],
                "organism": passport["organism"],
                "uniprot_id": passport["uniprot_id"],
                "disease_context": passport["disease_context"],
            },
            f"https://www.uniprot.org/uniprotkb/{passport['uniprot_id']}/entry",
        )
        upsert_entity(connection, site_id, "reactive_site", site["site_id"], site)

        for evidence in passport["citations"]:
            upsert_evidence(
                connection,
                evidence["id"],
                evidence["source_type"],
                evidence["title"],
                evidence["url"],
                None,
                {
                    "doi": evidence.get("doi"),
                    "pmid": evidence.get("pmid"),
                    "retrieved_on": evidence["retrieved_on"],
                    "supports": evidence["supports"],
                },
            )
        context_evidence = passport["citations"][0]["id"]
        upsert_relation(connection, project_id, "studies", target_id, context_evidence, 1.0)
        upsert_relation(connection, site_id, "belongs_to", target_id, context_evidence, 1.0)

        connection.execute(
            """
            INSERT INTO hypotheses(
                id, statement, status, confidence, assumptions_json,
                disconfirming_evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                statement=excluded.statement,
                status=excluded.status,
                confidence=excluded.confidence,
                assumptions_json=excluded.assumptions_json,
                disconfirming_evidence_json=excluded.disconfirming_evidence_json,
                updated_at=excluded.updated_at
            """,
            (
                hypothesis["id"],
                hypothesis["statement"],
                hypothesis["status"],
                hypothesis["confidence"],
                json_text(hypothesis["assumptions"]),
                json_text(hypothesis["disconfirming_evidence"]),
                now(),
                now(),
            ),
        )
        for evidence_id in hypothesis["supporting_evidence"]:
            connection.execute(
                """
                INSERT OR IGNORE INTO hypothesis_evidence(hypothesis_id, evidence_id, stance)
                VALUES (?, ?, 'supports')
                """,
                (hypothesis["id"], evidence_id),
            )

        artifact_count = 0
        for artifact in sorted(absolute_project.rglob("*.json")):
            relative = str(artifact.relative_to(root))
            digest = sha256_file(artifact)
            artifact_id = stable_id("artifact", relative, digest)
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, artifact_type, path, sha256, producer, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    id=excluded.id,
                    artifact_type=excluded.artifact_type,
                    sha256=excluded.sha256,
                    producer=excluded.producer,
                    metadata_json=excluded.metadata_json
                """,
                (
                    artifact_id,
                    artifact.stem,
                    relative,
                    digest,
                    "covalent-project-v1",
                    json_text({"project_id": project["project_id"]}),
                    now(),
                ),
            )
            artifact_count += 1
        connection.commit()
    return {
        "database": str(db_path),
        "project_id": project["project_id"],
        "evidence_ingested": len(passport["citations"]),
        "artifacts_ingested": artifact_count,
        "decision": assessment["decision"],
        "route": route["selected_route"],
        "status": "PASS",
    }


def table_counts(connection: sqlite3.Connection, tables: Iterable[str]) -> dict[str, int]:
    return {
        table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


def summary(db_path: Path) -> dict[str, Any]:
    with connect(db_path) as connection:
        counts = table_counts(
            connection,
            (
                "entities",
                "evidence",
                "relations",
                "hypotheses",
                "artifacts",
                "benchmark_runs",
            ),
        )
        runs = [dict(row) for row in connection.execute(
            """
            SELECT benchmark_id, freeze_id, engine, status, metrics_path
            FROM benchmark_runs ORDER BY engine
            """
        )]
    return {"database": str(db_path), "counts": counts, "benchmark_runs": runs}


def audit(db_path: Path, project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    errors: list[str] = []
    checks: list[dict[str, Any]] = []
    with connect(db_path) as connection:
        for row in connection.execute("SELECT path, sha256 FROM artifacts ORDER BY path"):
            path = root / row["path"]
            exists = path.is_file()
            digest = sha256_file(path) if exists else None
            status = "PASS" if exists and digest == row["sha256"] else "FAIL"
            checks.append({"path": row["path"], "status": status})
            if status != "PASS":
                errors.append(f"artifact missing or changed: {row['path']}")
        foreign_key_errors = [tuple(row) for row in connection.execute("PRAGMA foreign_key_check")]
        if foreign_key_errors:
            errors.append(f"foreign key errors: {len(foreign_key_errors)}")
    return {
        "database": str(db_path),
        "artifact_checks": checks,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
    }


def export_graph(db_path: Path, output_path: Path) -> dict[str, Any]:
    tables = (
        "metadata",
        "entities",
        "evidence",
        "relations",
        "hypotheses",
        "hypothesis_evidence",
        "artifacts",
        "benchmark_runs",
    )
    with connect(db_path) as connection:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "exported_at": now(),
            "tables": {
                table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in tables
            },
        }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return {"output": str(output_path), "status": "PASS"}


def print_result(result: dict[str, Any], as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the auditable scientific state graph.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "summary"):
        command = subparsers.add_parser(name)
        command.add_argument("--db", type=Path, required=True)
    ingest = subparsers.add_parser("ingest-btk")
    ingest.add_argument("--db", type=Path, required=True)
    ingest.add_argument("--project-root", type=Path, default=Path("."))
    ingest.add_argument(
        "--manifest",
        type=Path,
        default=Path("benchmark/frozen/BTK-mini-v1/manifest.json"),
    )
    ingest_generic = subparsers.add_parser("ingest-project")
    ingest_generic.add_argument("--db", type=Path, required=True)
    ingest_generic.add_argument("--project-root", type=Path, default=Path("."))
    ingest_generic.add_argument("--project-dir", type=Path, required=True)
    audit_parser = subparsers.add_parser("audit")
    audit_parser.add_argument("--db", type=Path, required=True)
    audit_parser.add_argument("--project-root", type=Path, default=Path("."))
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--db", type=Path, required=True)
    export_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        if args.command == "init":
            result = init_database(args.db)
        elif args.command == "ingest-btk":
            result = ingest_btk(args.db, args.project_root, args.manifest)
        elif args.command == "ingest-project":
            result = ingest_project(args.db, args.project_root, args.project_dir)
        elif args.command == "summary":
            result = summary(args.db)
        elif args.command == "audit":
            result = audit(args.db, args.project_root)
        else:
            result = export_graph(args.db, args.output)
    except Exception as exc:
        result = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
        print_result(result)
        return 1
    print_result(result)
    return 0 if result.get("status", "PASS") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
