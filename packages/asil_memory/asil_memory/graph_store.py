"""Thin sync wrapper around the Neo4j driver.

Why sync, not async? Phase 1 only writes from CLI ingest jobs (sequential)
and reads from CLI queries (interactive). The neo4j async driver doesn't add
throughput here, only ceremony. Phase 2+ may layer an async wrapper on top
when the API/MCP endpoints start hitting this in concurrent paths.

Why this layer at all? Two reasons:
  1. Centralizes connection lifecycle + retry semantics so callers can't
     leak sessions.
  2. Lets the graph_builder talk in domain operations (`merge_repo`,
     `merge_function_batch`) rather than raw Cypher, keeping the schema
     authoritative in one place.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from asil_core import get_settings
from asil_core.logging import get_logger

log = get_logger(__name__)


class GraphStoreError(RuntimeError):
    """Raised on connectivity / Cypher errors that callers shouldn't try to handle."""


@dataclass(slots=True)
class GraphConnection:
    uri: str
    user: str
    password: str
    database: str = "neo4j"


# Cypher constraints defining node identity. Idempotent — re-running `apply_schema`
# is safe. `repo_key` is a denormalized "<org>/<name>" or "local:<absolute_path>"
# string carried on every domain node so a single property filter scopes any
# query to one repo.
SCHEMA_CYPHER: tuple[str, ...] = (
    """
    CREATE CONSTRAINT asil_repo_unique IF NOT EXISTS
    FOR (r:Repo) REQUIRE r.key IS UNIQUE
    """,
    """
    CREATE CONSTRAINT asil_file_unique IF NOT EXISTS
    FOR (f:File) REQUIRE (f.repo_key, f.path) IS UNIQUE
    """,
    """
    CREATE CONSTRAINT asil_function_unique IF NOT EXISTS
    FOR (fn:Function) REQUIRE (fn.repo_key, fn.qualified_name) IS UNIQUE
    """,
    """
    CREATE CONSTRAINT asil_class_unique IF NOT EXISTS
    FOR (c:Class) REQUIRE (c.repo_key, c.qualified_name) IS UNIQUE
    """,
    """
    CREATE CONSTRAINT asil_symbol_unique IF NOT EXISTS
    FOR (s:Symbol) REQUIRE (s.repo_key, s.qualified_name) IS UNIQUE
    """,
)


class GraphStore:
    def __init__(self, conn: GraphConnection | None = None) -> None:
        # Defer the driver import so test code that never touches Neo4j
        # doesn't need the package installed.
        from neo4j import GraphDatabase

        if conn is None:
            s = get_settings()
            conn = GraphConnection(
                uri=s.neo4j_uri,
                user=s.neo4j_user,
                password=s.neo4j_password,
            )
        self._conn = conn
        try:
            # Tighten the default 60s deadline. If Bolt doesn't respond in 10s
            # the issue is structural (wrong port, container half-broken, auth
            # mismatch) and faster failure helps the dev loop.
            #
            self._driver = GraphDatabase.driver(
                conn.uri,
                auth=(conn.user, conn.password),
                connection_timeout=10.0,
                connection_acquisition_timeout=15.0,
            )
            # Silence the driver's "label X doesn't exist" / "property Y
            # doesn't exist" advisories — they fire on an empty or sparsely-
            # populated graph and are pure noise on stderr at our schema's
            # current lifecycle stage. Targeted at the well-known neo4j
            # logger so we don't lose actual errors.
            import logging as _logging

            _logging.getLogger("neo4j.notifications").setLevel(_logging.ERROR)
            _logging.getLogger("neo4j").setLevel(_logging.ERROR)
        except Exception as e:
            raise GraphStoreError(f"failed to construct Neo4j driver: {e}") from e

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> GraphStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def verify_connectivity(self) -> None:
        """Raises GraphStoreError if Neo4j is unreachable. Cheap; call from CLI startup."""
        try:
            self._driver.verify_connectivity()
        except Exception as e:
            raise GraphStoreError(
                f"can't reach Neo4j at {self._conn.uri}: {e}. Is `make up` running?"
            ) from e

    @contextmanager
    def _session(self):  # type: ignore[no-untyped-def]
        with self._driver.session(database=self._conn.database) as s:
            yield s

    # ------------------------------------------------------------------ schema

    def apply_schema(self) -> None:
        """Create constraints idempotently. Safe to call on every ingest."""
        with self._session() as s:
            for stmt in SCHEMA_CYPHER:
                s.run(stmt).consume()
        log.info("graph_schema_applied", constraints=len(SCHEMA_CYPHER))

    # ------------------------------------------------------------------ writes

    def merge_repo(
        self,
        *,
        key: str,
        spec: str,
        org: str | None,
        name: str | None,
        is_local: bool,
        commit_sha: str | None,
        indexed_at: str,
    ) -> None:
        cypher = """
        MERGE (r:Repo {key: $key})
        SET r.spec = $spec,
            r.org = $org,
            r.name = $name,
            r.is_local = $is_local,
            r.commit_sha = $commit_sha,
            r.indexed_at = $indexed_at
        """
        with self._session() as s:
            s.run(
                cypher,
                key=key,
                spec=spec,
                org=org,
                name=name,
                is_local=is_local,
                commit_sha=commit_sha,
                indexed_at=indexed_at,
            ).consume()

    def merge_file_with_children(
        self,
        *,
        repo_key: str,
        file_props: dict[str, Any],
        functions: list[dict[str, Any]],
        classes: list[dict[str, Any]],
        symbols: list[dict[str, Any]],
    ) -> None:
        """Upsert one File and all its child Functions/Classes/Symbols + CONTAINS edges.

        Single transaction per file → re-ingesting one file is atomic from the
        graph's point of view (no half-updated state).
        """
        cypher = """
        MERGE (r:Repo {key: $repo_key})
        WITH r
        MERGE (f:File {repo_key: $repo_key, path: $file.path})
        SET f += $file
        MERGE (r)-[:CONTAINS]->(f)

        WITH f
        UNWIND $functions AS fnp
          MERGE (fn:Function {repo_key: $repo_key, qualified_name: fnp.qualified_name})
          SET fn += fnp
          MERGE (f)-[:CONTAINS]->(fn)

        WITH f
        UNWIND $classes AS cp
          MERGE (c:Class {repo_key: $repo_key, qualified_name: cp.qualified_name})
          SET c += cp
          MERGE (f)-[:CONTAINS]->(c)
          // hook methods into both their class AND their file (already linked above)
          WITH f, c, cp
          UNWIND cp.method_qnames AS mqn
            MATCH (m:Function {repo_key: $repo_key, qualified_name: mqn})
            MERGE (c)-[:CONTAINS]->(m)

        WITH f
        UNWIND $symbols AS sp
          MERGE (s:Symbol {repo_key: $repo_key, qualified_name: sp.qualified_name})
          SET s += sp
          MERGE (f)-[:CONTAINS]->(s)
        """
        with self._session() as s:
            s.run(
                cypher,
                repo_key=repo_key,
                file=file_props,
                functions=functions,
                classes=classes,
                symbols=symbols,
            ).consume()

    def clear_repo(self, repo_key: str) -> int:
        """Detach-delete every node carrying this repo_key. Returns nodes removed."""
        cypher = """
        MATCH (n {repo_key: $repo_key})
        WITH n, count(n) AS _c
        DETACH DELETE n
        RETURN count(*) AS removed
        """
        with self._session() as s:
            record = s.run(cypher, repo_key=repo_key).single()
            removed = int(record["removed"]) if record else 0
        # Repo node itself uses `key` not `repo_key`; remove separately.
        cypher_repo = "MATCH (r:Repo {key: $key}) DETACH DELETE r"
        with self._session() as s:
            s.run(cypher_repo, key=repo_key).consume()
        return removed

    # ------------------------------------------------------------------ reads

    def stats(self, repo_key: str | None = None) -> dict[str, int]:
        """Counts of each domain label, optionally scoped to one repo."""
        labels = ("Repo", "File", "Function", "Class", "Symbol")
        out: dict[str, int] = {}
        with self._session() as s:
            for label in labels:
                if label == "Repo":
                    cypher = (
                        "MATCH (r:Repo {key: $key}) RETURN count(r) AS n"
                        if repo_key is not None
                        else "MATCH (r:Repo) RETURN count(r) AS n"
                    )
                    params = {"key": repo_key} if repo_key is not None else {}
                else:
                    cypher = (
                        f"MATCH (n:{label} {{repo_key: $key}}) RETURN count(n) AS n"
                        if repo_key is not None
                        else f"MATCH (n:{label}) RETURN count(n) AS n"
                    )
                    params = {"key": repo_key} if repo_key is not None else {}
                record = s.run(cypher, **params).single()
                out[label] = int(record["n"]) if record else 0
        return out

    def list_repos(self) -> list[dict[str, Any]]:
        cypher = """
        MATCH (r:Repo)
        OPTIONAL MATCH (r)-[:CONTAINS]->(f:File)
        WITH r, count(f) AS files
        RETURN r.key AS key, r.spec AS spec, r.commit_sha AS commit_sha,
               r.is_local AS is_local, r.indexed_at AS indexed_at, files
        ORDER BY r.indexed_at DESC
        """
        with self._session() as s:
            return [dict(record) for record in s.run(cypher)]

    def query(self, cypher: str, **params: Any) -> list[dict[str, Any]]:
        """Escape hatch for ad-hoc Cypher. Used by debug CLI commands; do NOT
        use this from agent code — go through typed methods so the schema stays
        discoverable in one place."""
        with self._session() as s:
            return [dict(record) for record in s.run(cypher, **params)]
