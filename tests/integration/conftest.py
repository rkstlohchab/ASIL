"""Integration test fixtures.

Skip individual tests if their backing service isn't reachable — keeps
`make test-unit` from needing docker while letting `make test-integration`
exercise the real stack.
"""

from __future__ import annotations

import pytest
from asil_memory import GraphStore, GraphStoreError, VectorStore, VectorStoreError


@pytest.fixture(scope="session")
def graph_store() -> GraphStore:
    store = GraphStore()
    try:
        store.verify_connectivity()
    except GraphStoreError as e:
        pytest.skip(f"neo4j unreachable: {e}")
    return store


@pytest.fixture(scope="session")
def vector_store() -> VectorStore:
    store = VectorStore()
    try:
        store.verify_connectivity()
    except VectorStoreError as e:
        pytest.skip(f"qdrant unreachable: {e}")
    yield store
    # Tests that touch the shared episodic collection size it for fake-vector
    # dims (4 or 8); the live CLI uses 1536 (OpenAI). Nuke the test residue so
    # subsequent CLI runs can recreate at the real dim.
    from asil_memory import EPISODIC_COLLECTION

    try:
        if store._client.collection_exists(EPISODIC_COLLECTION):  # type: ignore[attr-defined]
            store._client.delete_collection(EPISODIC_COLLECTION)  # type: ignore[attr-defined]
    except Exception:
        pass
    store.close()
