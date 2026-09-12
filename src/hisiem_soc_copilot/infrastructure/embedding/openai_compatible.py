"""OpenAI-compatible embedding adapter (brief sections 18/19/69).

Layering::

    Knowledge application service  →  EmbeddingProvider Protocol (application/ports)
    OpenAICompatibleEmbeddingAdapter (this module)  →  POST {base_url}/embeddings

The Application layer owns the *meaning* of an embedding (space identity, ordering,
fail-closed validation); this adapter owns only the *wire*. It is deliberately thin
and honest:

- configuration is passed explicitly (the composition root wires settings to these
  parameters — this module reads no env vars and holds no settings object);
- construction fails closed BEFORE any network call when the key/URL/model is blank,
  so an unconfigured deployment can never silently emit unauthenticated traffic;
- one bounded retry loop, and ONLY over transient faults (timeout / rate limit /
  5xx / transport). A malformed response is a broken provider, not a blip, so it is
  never retried;
- every failure becomes an :class:`ExternalServiceError` with a distinct, actionable
  code and a short message that leaks no secret, no request body and no raw response.

Secrets: the API key lives only in the per-request ``Authorization`` header. It is
never logged, never placed in an exception message, and never stored on a record.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import httpx

from ...application.errors import ExternalServiceError
from ...application.ports.embedding import (
    EmbeddingBatch,
    EmbeddingProfileDescriptor,
    EmbeddingProvider,
    EmbeddingVector,
)
from ...domain.knowledge.errors import InvalidEmbeddingVectorError

_PROVIDER = "openai_compatible"
_SERVICE = "embedding"

# The failure codes that describe a TRANSIENT fault and may therefore be retried.
# Everything else (notably EMBEDDING_MALFORMED_RESPONSE and EMBEDDING_NOT_CONFIGURED)
# is deterministic: retrying it only wastes time and hides the real bug.
_RETRYABLE_CODES = frozenset(
    {
        "EMBEDDING_TIMEOUT",
        "EMBEDDING_RATE_LIMITED",
        "EMBEDDING_UPSTREAM_ERROR",
        "EMBEDDING_UNAVAILABLE",
    }
)

# Upper bound on a single backoff sleep, so a long retry ladder cannot stall a
# request for minutes.
_MAX_BACKOFF_SECONDS = 4.0


class OpenAICompatibleEmbeddingAdapter:
    """Embedding adapter implementing the ``EmbeddingProvider`` Protocol.

    Not a subclass of the Protocol — the port is structural, and a reader should be
    able to see the whole contract from this class alone.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_id: str,
        dimension: int,
        normalization: str = "NONE",
        distance_metric: str = "COSINE",
        profile_version: int = 1,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # Fail closed before building anything. A blank credential or endpoint is a
        # deployment defect: refuse loudly here rather than send an unauthenticated
        # request and map a confusing upstream 401 later.
        if not base_url or not base_url.strip():
            raise _not_configured()
        if not api_key or not api_key.strip():
            raise _not_configured()
        if not model_id or not model_id.strip():
            raise _not_configured()
        if max_retries < 0:
            raise _not_configured()
        self._base_url = base_url
        self._api_key = api_key
        self._model_id = model_id
        self._timeout_seconds = float(timeout_seconds)
        self._max_retries = int(max_retries)
        # An injected client is owned by the caller and is never closed by us; when
        # none is given, each call builds and closes its own short-lived client.
        self._client = client
        # The descriptor validates dimension/normalization/distance_metric itself and
        # raises InvalidEmbeddingVectorError — the same fail-closed contract the rest
        # of the knowledge subsystem relies on.
        self._descriptor = EmbeddingProfileDescriptor(
            provider=_PROVIDER,
            model_id=model_id,
            dimension=dimension,
            distance_metric=distance_metric,
            normalization=normalization,
            profile_version=profile_version,
        )

    @property
    def descriptor(self) -> EmbeddingProfileDescriptor:
        """Identity of the embedding space every vector from this adapter carries."""
        return self._descriptor

    async def aclose(self) -> None:
        """Release a self-created client, if one is held.

        Clients we build are per-call and closed in a ``finally``, so there is
        normally nothing to release; an injected client belongs to the caller and is
        deliberately left open. The method exists for symmetry with the other
        adapters and to be a safe no-op in both cases.
        """
        return None

    # ------------------------------------------------------------------
    # EmbeddingProvider
    # ------------------------------------------------------------------
    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Embed a batch of documents, preserving input order.

        An empty batch is rejected without a request: an empty embedding result is a
        caller bug, not a provider answer.
        """
        if not texts:
            raise InvalidEmbeddingVectorError(
                "embed_documents requires at least one text"
            )
        inputs = [str(text) for text in texts]
        entries = await self._embed(inputs)
        # Count and order are checked HERE (as a provider-fault code) rather than left
        # to the DTO: a provider that reorders or drops vectors is misbehaving, and we
        # must refuse to pair a vector with the wrong source text.
        if len(entries) != len(inputs):
            raise _malformed(
                f"embedding provider returned {len(entries)} vectors "
                f"for {len(inputs)} inputs"
            )
        if [index for index, _ in entries] != list(range(len(inputs))):
            raise _malformed("embedding provider returned vectors out of input order")
        # EmbeddingVector's own validation now fails closed on dimension, emptiness
        # and non-finite values; those exceptions propagate unchanged.
        vectors = tuple(
            EmbeddingVector(values=tuple(values), descriptor=self._descriptor, index=index)
            for index, values in entries
        )
        return EmbeddingBatch(descriptor=self._descriptor, vectors=vectors)

    async def embed_query(self, text: str) -> EmbeddingVector:
        """Embed a single query string; the returned vector has ``index=None``."""
        if not text or not text.strip():
            raise InvalidEmbeddingVectorError(
                "embed_query requires a non-empty query text"
            )
        entries = await self._embed([text])
        if len(entries) != 1 or entries[0][0] != 0:
            raise _malformed("embedding provider returned an unexpected number of vectors")
        _, values = entries[0]
        return EmbeddingVector(
            values=tuple(values), descriptor=self._descriptor, index=None
        )

    # ------------------------------------------------------------------
    # transport + bounded retry
    # ------------------------------------------------------------------
    async def _embed(self, inputs: list[str]) -> list[tuple[int, list[float]]]:
        """One logical embedding call, retried only over transient faults."""
        url = f"{self._base_url.rstrip('/')}/embeddings"
        body: dict[str, object] = {
            "model": self._model_id,
            "input": inputs,
            "encoding_format": "float",
        }
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._post(url, body)
                _raise_for_status(response)
                return _parse_embeddings(response)
            except ExternalServiceError as exc:
                if exc.upstream_code in _RETRYABLE_CODES and attempt <= self._max_retries:
                    # Bounded exponential backoff: 1s, 2s, 4s, … capped. A transient
                    # upstream blip deserves a second chance; a malformed payload does
                    # not (it is not in _RETRYABLE_CODES and propagates immediately).
                    await asyncio.sleep(min(0.5 * 2**attempt, _MAX_BACKOFF_SECONDS))
                    continue
                raise

    async def _post(self, url: str, body: dict[str, object]) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        client = self._client
        owned = client is None
        if client is None:
            client = httpx.AsyncClient(timeout=self._timeout_seconds)
        try:
            return await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            # Checked before httpx.HTTPError: a timeout IS an HTTPError, but it has
            # its own actionable code.
            raise _error("embedding provider request timed out", "EMBEDDING_TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise _error("embedding provider is unreachable", "EMBEDDING_UNAVAILABLE") from exc
        finally:
            if owned:
                await client.aclose()


# ---------------------------------------------------------------------------
# error construction + response parsing (module-level, side-effect free)
# ---------------------------------------------------------------------------


def _error(message: str, code: str) -> ExternalServiceError:
    return ExternalServiceError(message, service=_SERVICE, code=code)


def _not_configured() -> ExternalServiceError:
    return _error("embedding provider is not configured", "EMBEDDING_NOT_CONFIGURED")


def _malformed(message: str) -> ExternalServiceError:
    return _error(message, "EMBEDDING_MALFORMED_RESPONSE")


def _raise_for_status(response: httpx.Response) -> None:
    """Map a non-2xx status onto the taxonomy, keeping ONLY the status code.

    The response body is never read or quoted: it may echo request content.
    """
    status = response.status_code
    if 200 <= status < 300:
        return
    if status == 429:
        raise _error("embedding provider rate-limited the request", "EMBEDDING_RATE_LIMITED")
    # 5xx and every other non-2xx collapse to one code: from the caller's side they
    # are the same fault (the provider could not answer), and both are retryable.
    raise _error(f"embedding provider returned HTTP {status}", "EMBEDDING_UPSTREAM_ERROR")


def _parse_embeddings(response: httpx.Response) -> list[tuple[int, list[float]]]:
    """Extract ``(index, values)`` pairs from an OpenAI-compatible response object.

    Structural problems (non-JSON, non-object root, missing/invalid ``data``, an
    entry without a numeric ``index``/``embedding``) are provider faults and raise
    EMBEDDING_MALFORMED_RESPONSE. Value-level faults (wrong dimension, empty vector,
    NaN/Infinity) are left to EmbeddingVector so there is exactly one place that
    decides what a usable vector is.
    """
    try:
        payload: object = response.json()
    except ValueError as exc:
        raise _malformed("embedding provider returned a non-JSON response") from exc
    if not isinstance(payload, dict):
        raise _malformed("embedding provider returned a malformed response")
    data: object = payload.get("data")
    if not isinstance(data, list):
        raise _malformed("embedding provider response is missing its data list")
    entries: list[tuple[int, list[float]]] = []
    for raw_item in data:
        item: object = raw_item
        if not isinstance(item, dict):
            raise _malformed("embedding provider returned a malformed data entry")
        raw_index: object = item.get("index")
        raw_embedding: object = item.get("embedding")
        if not isinstance(raw_index, int) or isinstance(raw_index, bool):
            raise _malformed("embedding provider returned a data entry without an index")
        if not isinstance(raw_embedding, list):
            raise _malformed("embedding provider returned a data entry without a vector")
        values: list[float] = []
        for raw_value in raw_embedding:
            value: object = raw_value
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise _malformed(
                    "embedding provider returned a non-numeric vector component"
                )
            values.append(float(value))
        entries.append((raw_index, values))
    return entries


def _implements_embedding_provider(
    adapter: OpenAICompatibleEmbeddingAdapter,
) -> EmbeddingProvider:
    """Static (mypy-only) proof that the adapter satisfies the port's structure.

    Never called at runtime: the port is structural, so this turns a mismatch into a
    type-check failure instead of a failure in the composition root.
    """
    return adapter
