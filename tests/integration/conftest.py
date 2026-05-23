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
    return store
