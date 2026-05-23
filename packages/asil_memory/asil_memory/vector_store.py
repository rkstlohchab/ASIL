"""Thin sync wrapper around the Qdrant client.

Mirror of GraphStore's shape: defer the client import, centralize connection
lifecycle, expose typed domain operations. One collection (`asil_code`) for
all repos with per-repo filtering by `repo_key` in the payload — keeps cross-
repo semantic search natural while still letting `asil vector clear <repo>`
scope cleanly.

Distance: cosine. Vector dim is configurable per-collection (different LLM
profiles use different embedding models — BGE-large 1024 vs Voyage-3-code
1024 vs text-embedding-3-small 1536). We validate the dim on first use.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from asil_core import get_settings
from asil_core.logging import get_logger

log = get_logger(__name__)

DEFAULT_COLLECTION = "asil_code"


class VectorStoreError(RuntimeError):
    """Connectivity / API errors that callers shouldn't try to handle."""


@dataclass(slots=True)
class VectorPoint:
    """One vector + its payload. `id` is deterministic on (repo_key, qualified_name)
    so re-ingest upserts cleanly instead of duplicating."""

    id: str
    vector: list[float]
    payload: dict[str, Any]


@dataclass(slots=True)
class SearchHit:
    id: str
    score: float
    payload: dict[str, Any]


def point_id_for(repo_key: str, qualified_name: str) -> str:
    """Deterministic UUID for a code node so re-ingest replaces rather than
    duplicates. Qdrant requires UUIDs or unsigned ints — we hash to UUID."""
    h = hashlib.sha1(f"{repo_key}::{qualified_name}".encode()).hexdigest()
    # Format as a UUID (8-4-4-4-12) — Qdrant accepts UUID strings.
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


class VectorStore:
    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
    ) -> None:
        from qdrant_client import QdrantClient

        if url is None:
            s = get_settings()
            url = s.qdrant_url
            api_key = api_key or s.qdrant_api_key

        self._url = url
        try:
            self._client = QdrantClient(
                url=url,
                # An empty string is treated as "set" by the client and emits an
                # "api key on insecure connection" warning. Pass None instead.
                api_key=api_key or None,
                timeout=10.0,
                # Qdrant warns when the client minor version drifts from the
                # server's; our docker pins the server at 1.12 and the client
                # is whatever uv resolves. Compatibility is fine in practice.
                check_compatibility=False,
            )
        except Exception as e:
            raise VectorStoreError(f"failed to construct Qdrant client: {e}") from e

        # Mirror GraphStore: silence qdrant's chatty INFO logs (we have our own).
        logging.getLogger("qdrant_client").setLevel(logging.WARNING)

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> VectorStore:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def verify_connectivity(self) -> None:
        """Raise VectorStoreError if Qdrant is unreachable."""
        try:
            self._client.get_collections()
        except Exception as e:
            raise VectorStoreError(
                f"can't reach Qdrant at {self._url}: {e}. Is `make up` running?"
            ) from e

    # ------------------------------------------------------------------ schema

    def ensure_collection(
        self,
        name: str = DEFAULT_COLLECTION,
        dim: int = 1536,
    ) -> None:
        """Create the collection if missing. If it exists with a different dim,
        raise — we don't auto-migrate (would silently drop existing vectors)."""
        from qdrant_client.http import models as qm

        existing = self._client.collection_exists(name)
        if not existing:
            self._client.create_collection(
                collection_name=name,
                vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
            )
            log.info("vector_collection_created", name=name, dim=dim)
            return

        info = self._client.get_collection(name)
        current_dim = info.config.params.vectors.size  # type: ignore[union-attr]
        if current_dim != dim:
            raise VectorStoreError(
                f"collection {name!r} exists with dim={current_dim}, "
                f"requested dim={dim}. Run `asil vector clear` or recreate "
                "the collection (embedding model probably changed)."
            )

    # ------------------------------------------------------------------ writes

    def upsert_batch(
        self,
        points: list[VectorPoint],
        collection: str = DEFAULT_COLLECTION,
    ) -> None:
        if not points:
            return
        from qdrant_client.http import models as qm

        self._client.upsert(
            collection_name=collection,
            points=[qm.PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in points],
            wait=False,
        )

    def clear_repo(
        self,
        repo_key: str,
        collection: str = DEFAULT_COLLECTION,
    ) -> int:
        """Delete every point belonging to `repo_key`. Returns 0 if collection
        doesn't exist yet — re-clearing is harmless."""
        if not self._client.collection_exists(collection):
            return 0
        from qdrant_client.http import models as qm

        before = self.count(collection, repo_key=repo_key)
        self._client.delete(
            collection_name=collection,
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[qm.FieldCondition(key="repo_key", match=qm.MatchValue(value=repo_key))]
                )
            ),
            wait=True,
        )
        return before

    # ------------------------------------------------------------------ reads

    def count(
        self,
        collection: str = DEFAULT_COLLECTION,
        *,
        repo_key: str | None = None,
    ) -> int:
        if not self._client.collection_exists(collection):
            return 0
        from qdrant_client.http import models as qm

        flt = (
            qm.Filter(must=[qm.FieldCondition(key="repo_key", match=qm.MatchValue(value=repo_key))])
            if repo_key
            else None
        )
        result = self._client.count(collection_name=collection, count_filter=flt, exact=True)
        return int(result.count)

    def stats(self, collection: str = DEFAULT_COLLECTION) -> dict[str, Any]:
        """Total point count + per-repo breakdown."""
        if not self._client.collection_exists(collection):
            return {"collection": collection, "exists": False, "total": 0, "per_repo": {}}

        total = self.count(collection)
        # Scroll through all points to bucket by repo_key. For Phase 1 this is
        # fine; if we ever ingest millions of files we'll switch to Qdrant's
        # grouping facet support (which exists but is collection-dependent).
        per_repo: dict[str, int] = {}
        offset: Any = None
        while True:
            batch, offset = self._client.scroll(
                collection_name=collection,
                limit=500,
                with_vectors=False,
                with_payload=["repo_key"],
                offset=offset,
            )
            for point in batch:
                rk = (point.payload or {}).get("repo_key", "<unknown>")
                per_repo[rk] = per_repo.get(rk, 0) + 1
            if offset is None:
                break
        return {
            "collection": collection,
            "exists": True,
            "total": total,
            "per_repo": per_repo,
        }

    def search(
        self,
        query_vector: list[float],
        *,
        limit: int = 10,
        repo_key: str | None = None,
        kind: str | None = None,
        collection: str = DEFAULT_COLLECTION,
    ) -> list[SearchHit]:
        """Top-k similar points. Optional filters: scope to one repo, or to
        one node kind (`function` | `class`)."""
        from qdrant_client.http import models as qm

        must: list[Any] = []
        if repo_key:
            must.append(qm.FieldCondition(key="repo_key", match=qm.MatchValue(value=repo_key)))
        if kind:
            must.append(qm.FieldCondition(key="kind", match=qm.MatchValue(value=kind)))
        flt = qm.Filter(must=must) if must else None

        # query_points supersedes the older `search` API in qdrant-client 1.10+.
        result = self._client.query_points(
            collection_name=collection,
            query=query_vector,
            limit=limit,
            query_filter=flt,
            with_payload=True,
        )
        return [
            SearchHit(id=str(p.id), score=float(p.score), payload=p.payload or {})
            for p in result.points
        ]
