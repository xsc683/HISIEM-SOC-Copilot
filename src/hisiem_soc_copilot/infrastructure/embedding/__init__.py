"""Embedding provider adapters (brief sections 18/19/69).

Two implementations of the Application-layer ``EmbeddingProvider`` port live here:

* ``OpenAICompatibleEmbeddingAdapter`` (``openai_compatible``) — the production
  adapter. It speaks the OpenAI-compatible ``POST /embeddings`` wire protocol over
  httpx and maps every failure onto the ``ExternalServiceError`` taxonomy.
* ``DeterministicEmbeddingProvider`` (``deterministic``) — a TEST FIXTURE ONLY.
  Its vectors are semantically meaningless and must never back a retrieval,
  ranking, or quality claim (see that module's warning).

Vendor SDK knowledge is confined to this package: the Application layer depends on
the port, and only these modules know how an embedding vendor actually answers.
"""
