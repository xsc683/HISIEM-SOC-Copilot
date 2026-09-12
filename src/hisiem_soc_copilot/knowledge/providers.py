"""Embedding-provider selection for the knowledge operator surfaces (section 19).

Every knowledge entry point that embeds anything -- the CLI commands and the
evaluation driver -- has to answer the same question first: *which embedding
provider is this run actually using?* They answer it here, in one place, because
that answer is what decides whether a number produced by the run means anything
at all. A run that reported one provider and embedded with another would produce
a baseline nobody could reproduce.

Two rules are enforced here rather than trusted to a caller:

* **The deterministic fixture is a test double and is never a default.** It is
  unreachable unless it is named in full on the command line, and its module is
  imported only on that branch -- so an ordinary invocation does not even load
  it. Section 19 is explicit that it must never be a production default, and the
  surest way to honor that is to make "use the fake" a deliberate keystroke.
* **No secret leaves this module.** The API key is read from the environment by
  the adapter and is never returned, formatted, or stored here. The only thing
  any caller can learn from this module is whether a provider is PRESENT.
"""

from __future__ import annotations

from ..application.errors import ApplicationError
from ..application.ports.embedding import EmbeddingProvider
from ..bootstrap.container import Container

#: The name an operator must type to reach the test double. Spelled out so it
#: cannot be mistaken for a production setting.
DETERMINISTIC_PROVIDER = "deterministic-test-only"

#: The choices the CLI accepts. ``configured`` means "whatever this deployment's
#: EMBEDDING_* settings say" -- the production path.
PROVIDER_CHOICES: tuple[str, ...] = ("configured", DETERMINISTIC_PROVIDER)


def resolve_embedding_provider(container: Container, choice: str) -> EmbeddingProvider | None:
    """Return the provider for ``choice``, or ``None`` when none is configured.

    ``None`` is a real answer, not an error: lexical retrieval does not need an
    embedding provider, and an operator running a lexical-only baseline on an
    already-ingested corpus must be able to do that. The callers that DO need one
    say so themselves, with :func:`require_embedding_provider`.
    """
    if choice == DETERMINISTIC_PROVIDER:
        # Imported lazily and only here: a production invocation never loads the
        # test double at all.
        from ..infrastructure.embedding.deterministic import DeterministicEmbeddingProvider

        return DeterministicEmbeddingProvider()
    return container.embedding_provider()


def require_embedding_provider(
    provider: EmbeddingProvider | None, *, what: str
) -> EmbeddingProvider:
    """Return ``provider``, or fail with the configuration an operator needs.

    The message names the settings rather than saying "not configured", because
    the failure this replaces -- an opaque AttributeError deep inside embedding --
    costs an operator far more time than the four setting names do.
    """
    if provider is None:
        raise ApplicationError(
            f"{what} requires an embedding provider; set EMBEDDING_PROVIDER, "
            "EMBEDDING_BASE_URL, EMBEDDING_MODEL and EMBEDDING_DIMENSION, or run with "
            f"--embedding-provider {DETERMINISTIC_PROVIDER} for a plumbing-only check"
        )
    return provider
