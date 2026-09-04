"""Durable investigation runtime — outbox dispatcher + graph runner.

Infrastructure-layer wiring that turns a committed ``investigation_created``
domain event (whose outbox row was written atomically with the investigation) into
an asynchronously executed LangGraph run:

    outbox_message (PENDING)
      → dispatcher claims it (own transaction)
      → runner: read domain investigation (own short transactions)
      → OrchestrationBinding (create once, deterministic thread_id)
      → AsyncPostgresSaver checkpointed graph runs/resumes to a terminal state
      → outbox marked PUBLISHED

No graph / LLM / tool / HTTP call ever runs inside a database transaction, and the
``copilot`` ORM session is never shared with the ``langgraph_checkpoint``
connection (persistence-schema.md §31, §37).
"""
