"""ASIL memory: storage primitives for the knowledge graph, vectors, and episodic store.

Phase 1 surface (this module):
  - graph_store: Neo4j client wrapper (constraints, MERGE primitives, queries)

Future phases:
  - vector_store: Qdrant wrapper (Phase 1.4)
  - episodic:     Mem0 wrapper (Phase 2)
  - hybrid_retriever: combines the above for unified retrieval (Phase 1.5)
"""

from asil_memory.episodic import (
    EPISODIC_COLLECTION,
    EpisodicStore,
    EpisodicStoreError,
    Memory,
    MemoryHit,
)
from asil_memory.graph_store import GraphStore, GraphStoreError
from asil_memory.hybrid_retriever import (
    HybridRetriever,
    RetrievalCandidate,
    RetrievalResult,
)
from asil_memory.teams import Team, TeamsStore, TeamWithKey
from asil_memory.vector_store import (
    DEFAULT_COLLECTION,
    SearchHit,
    VectorPoint,
    VectorStore,
    VectorStoreError,
    point_id_for,
)

__version__ = "0.0.1"

__all__ = [
    "DEFAULT_COLLECTION",
    "EPISODIC_COLLECTION",
    "EpisodicStore",
    "EpisodicStoreError",
    "GraphStore",
    "GraphStoreError",
    "HybridRetriever",
    "Memory",
    "MemoryHit",
    "RetrievalCandidate",
    "RetrievalResult",
    "SearchHit",
    "Team",
    "TeamWithKey",
    "TeamsStore",
    "VectorPoint",
    "VectorStore",
    "VectorStoreError",
    "point_id_for",
]
