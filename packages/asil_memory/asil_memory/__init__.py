"""ASIL memory: storage primitives for the knowledge graph, vectors, and episodic store.

Phase 1 surface (this module):
  - graph_store: Neo4j client wrapper (constraints, MERGE primitives, queries)

Future phases:
  - vector_store: Qdrant wrapper (Phase 1.4)
  - episodic:     Mem0 wrapper (Phase 2)
  - hybrid_retriever: combines the above for unified retrieval (Phase 1.5)
"""

from asil_memory.graph_store import GraphStore, GraphStoreError

__version__ = "0.0.1"

__all__ = ["GraphStore", "GraphStoreError"]
