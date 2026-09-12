"""Embedding provider adapter tests (brief sections 17/18/69).

Everything here runs offline: the production adapter is driven through an
``httpx.MockTransport`` so no socket is ever opened, and the deterministic provider
is exercised as the plumbing fixture it is. These tests prove the WIRE contract and
the fail-closed validation — never retrieval or semantic quality.
"""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from hisiem_soc_copilot.application.errors import ExternalServiceError
from hisiem_soc_copilot.application.ports.embedding import (
    EmbeddingBatch,
    EmbeddingProvider,
    EmbeddingVector,
)
from hisiem_soc_copilot.domain.knowledge.errors import InvalidEmbeddingVectorError
from hisiem_soc_copilot.infrastructure.embedding import deterministic, openai_compatible
from hisiem_soc_copilot.infrastructure.embedding.deterministic import (
    DeterministicEmbeddingProvider,
)
from hisiem_soc_copilot.infrastructure.embedding.openai_compatible import (
    OpenAICompatibleEmbeddingAdapter,
)

Handler = Callable[[httpx.Request], httpx.Response]

_BASE_URL = "https://embedding.test/v1"
_MODEL_ID = "text-embedding-test"
_DIMENSION = 4
_API_KEY = "unit-test-secret-key"

# Dyadic values so the parsed floats compare exactly.
_GOOD_VECTORS: tuple[list[float], ...] = (
    [0.5, 0.25, 0.125, 0.0625],
    [1.0, -0.5, 0.25, -0.125],
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _payload(vectors: list[list[float]], indices: list[int] | None = None) -> dict[str, Any]:
    """An OpenAI-compatible ``/embeddings`` response body."""
    order = indices if indices is not None else list(range(len(vectors)))
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in zip(order, vectors, strict=True)
        ],
        "model": _MODEL_ID,
    }


def _build_adapter(
    client: httpx.AsyncClient, **overrides: object
) -> OpenAICompatibleEmbeddingAdapter:
    kwargs: dict[str, object] = {
        "base_url": _BASE_URL,
        "api_key": _API_KEY,
        "model_id": _MODEL_ID,
        "dimension": _DIMENSION,
        "client": client,
    }
    kwargs.update(overrides)
    return OpenAICompatibleEmbeddingAdapter(**kwargs)  # type: ignore[arg-type]


@asynccontextmanager
async def _adapter(
    handler: Handler, **overrides: object
) -> AsyncIterator[OpenAICompatibleEmbeddingAdapter]:
    """An adapter over a MockTransport, with the client always closed afterwards."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        yield _build_adapter(client, **overrides)
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# DeterministicEmbeddingProvider (fixture)
# ---------------------------------------------------------------------------


def test_deterministic_descriptor_identity() -> None:
    provider: EmbeddingProvider = DeterministicEmbeddingProvider()
    assert provider.descriptor.provider == "deterministic"
    assert provider.descriptor.model_id == "deterministic-test-v1"
    assert provider.descriptor.normalization == "L2"


async def test_deterministic_is_stable_across_calls_and_instances() -> None:
    first = await DeterministicEmbeddingProvider().embed_query("lateral movement")
    second = await DeterministicEmbeddingProvider().embed_query("lateral movement")
    assert first.values == second.values
    assert first.descriptor.identity == second.descriptor.identity


@pytest.mark.parametrize("dimension", [1, 4, 64, 1536])
async def test_deterministic_dimension_and_raw_range(dimension: int) -> None:
    provider = DeterministicEmbeddingProvider(dimension=dimension, normalization="NONE")
    vector = await provider.embed_query("a brute-force login burst")
    assert len(vector.values) == dimension
    assert all(math.isfinite(value) for value in vector.values)
    assert all(-1.0 <= value < 1.0 for value in vector.values)


async def test_deterministic_l2_vectors_have_unit_norm() -> None:
    provider = DeterministicEmbeddingProvider(dimension=32)
    for text in ("alpha", "beta", "S1 credential dumping"):
        vector = await provider.embed_query(text)
        norm = math.sqrt(sum(value * value for value in vector.values))
        assert abs(norm - 1.0) < 1e-9


async def test_deterministic_distinct_texts_produce_distinct_vectors() -> None:
    provider = DeterministicEmbeddingProvider()
    texts = ["alpha", "beta", "gamma", "delta", "epsilon"]
    vectors = [await provider.embed_query(text) for text in texts]
    assert len({vector.values for vector in vectors}) == len(texts)


async def test_deterministic_documents_have_contiguous_indices() -> None:
    provider = DeterministicEmbeddingProvider()
    batch = await provider.embed_documents(["one", "two", "three"])
    assert isinstance(batch, EmbeddingBatch)
    assert [vector.index for vector in batch.vectors] == [0, 1, 2]
    assert all(
        vector.descriptor.identity == provider.descriptor.identity
        for vector in batch.vectors
    )


async def test_deterministic_query_has_no_index() -> None:
    vector = await DeterministicEmbeddingProvider().embed_query("query")
    assert isinstance(vector, EmbeddingVector)
    assert vector.index is None


async def test_deterministic_empty_documents_raise() -> None:
    with pytest.raises(InvalidEmbeddingVectorError):
        await DeterministicEmbeddingProvider().embed_documents([])


@pytest.mark.parametrize("text", ["", "   ", "\t\n"])
async def test_deterministic_blank_query_raises(text: str) -> None:
    with pytest.raises(InvalidEmbeddingVectorError):
        await DeterministicEmbeddingProvider().embed_query(text)


def test_deterministic_zero_vector_stays_zero() -> None:
    # The divide-by-zero guard on the L2 path: a zero vector is not normalisable.
    assert deterministic._l2_normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


# ---------------------------------------------------------------------------
# OpenAICompatibleEmbeddingAdapter — happy path
# ---------------------------------------------------------------------------


async def test_adapter_embed_documents_happy_path() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        body = json.loads(request.content)
        seen["model"] = body.get("model")
        seen["input"] = body.get("input")
        seen["encoding_format"] = body.get("encoding_format")
        auth = request.headers.get("authorization", "")
        # Boolean, so a failure never echoes the credential into the assertion diff.
        seen["bearer_ok"] = auth.startswith("Bearer ") and auth[7:] == _API_KEY
        return httpx.Response(200, json=_payload([list(_GOOD_VECTORS[0]), list(_GOOD_VECTORS[1])]))

    async with _adapter(handler) as adapter:
        batch = await adapter.embed_documents(["first", "second"])

    assert seen["path"] == "/v1/embeddings"
    assert seen["model"] == _MODEL_ID
    assert seen["input"] == ["first", "second"]
    assert seen["encoding_format"] == "float"
    assert seen["bearer_ok"] is True
    assert isinstance(batch, EmbeddingBatch)
    assert batch.descriptor == adapter.descriptor
    assert batch.descriptor.provider == "openai_compatible"
    assert len(batch.vectors) == 2
    assert [vector.index for vector in batch.vectors] == [0, 1]
    assert batch.vectors[0].values == tuple(_GOOD_VECTORS[0])
    assert all(isinstance(value, float) for value in batch.vectors[0].values)


async def test_adapter_embed_query_happy_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["input"] == ["pivot to domain controller"]
        return httpx.Response(200, json=_payload([list(_GOOD_VECTORS[1])]))

    async with _adapter(handler) as adapter:
        vector = await adapter.embed_query("pivot to domain controller")

    assert isinstance(vector, EmbeddingVector)
    assert vector.index is None
    assert vector.values == tuple(_GOOD_VECTORS[1])


# ---------------------------------------------------------------------------
# OpenAICompatibleEmbeddingAdapter — malformed / invalid payloads
# ---------------------------------------------------------------------------


async def test_adapter_wrong_vector_count_is_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_payload([list(_GOOD_VECTORS[0])]))

    async with _adapter(handler) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["first", "second"])
    assert exc.value.upstream_code == "EMBEDDING_MALFORMED_RESPONSE"


async def test_adapter_reordered_indices_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _payload([list(_GOOD_VECTORS[0]), list(_GOOD_VECTORS[1])], indices=[1, 0])
        return httpx.Response(200, json=payload)

    async with _adapter(handler) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["first", "second"])
    assert exc.value.upstream_code == "EMBEDDING_MALFORMED_RESPONSE"


async def test_adapter_dimension_mismatch_raises_invalid_vector() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_payload([[0.1, 0.2, 0.3]]))  # 3 != dimension 4

    async with _adapter(handler) as adapter:
        with pytest.raises(InvalidEmbeddingVectorError):
            await adapter.embed_documents(["only"])


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
async def test_adapter_non_finite_values_raise(bad: float) -> None:
    # httpx's JSON encoder refuses non-finite floats, so the body is built by hand:
    # NaN/Infinity ARE parseable JSON extensions, and the adapter must reject them.
    raw = json.dumps(_payload([[0.1, 0.2, 0.3, bad]]), allow_nan=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=raw, headers={"content-type": "application/json"}
        )

    async with _adapter(handler) as adapter:
        with pytest.raises(InvalidEmbeddingVectorError):
            await adapter.embed_query("query")


async def test_adapter_empty_vector_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_payload([[]]))

    async with _adapter(handler) as adapter:
        with pytest.raises(InvalidEmbeddingVectorError):
            await adapter.embed_query("query")


async def test_adapter_non_numeric_component_is_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = _payload([[0.1, 0.2, 0.3, 0.4]])
        payload["data"][0]["embedding"] = [0.1, "not-a-number", 0.3, 0.4]
        return httpx.Response(200, json=payload)

    async with _adapter(handler) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_query("query")
    assert exc.value.upstream_code == "EMBEDDING_MALFORMED_RESPONSE"


# ---------------------------------------------------------------------------
# OpenAICompatibleEmbeddingAdapter — error taxonomy
# ---------------------------------------------------------------------------


async def test_adapter_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    async with _adapter(handler, max_retries=0) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["a"])
    assert exc.value.upstream_code == "EMBEDDING_RATE_LIMITED"


async def test_adapter_server_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    async with _adapter(handler, max_retries=0) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["a"])
    assert exc.value.upstream_code == "EMBEDDING_UPSTREAM_ERROR"


async def test_adapter_other_non_2xx_is_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    async with _adapter(handler, max_retries=0) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["a"])
    assert exc.value.upstream_code == "EMBEDDING_UPSTREAM_ERROR"


async def test_adapter_non_json_body_is_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with _adapter(handler) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["a"])
    assert exc.value.upstream_code == "EMBEDDING_MALFORMED_RESPONSE"


async def test_adapter_non_object_root_is_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    async with _adapter(handler) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["a"])
    assert exc.value.upstream_code == "EMBEDDING_MALFORMED_RESPONSE"


async def test_adapter_missing_data_is_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"object": "list"})

    async with _adapter(handler) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["a"])
    assert exc.value.upstream_code == "EMBEDDING_MALFORMED_RESPONSE"


async def test_adapter_transport_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _adapter(handler, max_retries=0) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["a"])
    assert exc.value.upstream_code == "EMBEDDING_UNAVAILABLE"


async def test_adapter_timeout_is_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    async with _adapter(handler, max_retries=0) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["a"])
    assert exc.value.upstream_code == "EMBEDDING_TIMEOUT"


# ---------------------------------------------------------------------------
# OpenAICompatibleEmbeddingAdapter — bounded retry policy
# ---------------------------------------------------------------------------


async def test_adapter_retries_transient_failures_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    # Patch the backoff sleep so the ladder is proven without actually waiting.
    monkeypatch.setattr(openai_compatible.asyncio, "sleep", _fake_sleep)

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) <= 2:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=_payload([list(_GOOD_VECTORS[0])]))

    async with _adapter(handler, max_retries=2) as adapter:
        batch = await adapter.embed_documents(["only"])

    assert len(calls) == 3
    assert batch.vectors[0].values == tuple(_GOOD_VECTORS[0])
    assert delays == [1.0, 2.0]  # min(0.5 * 2**attempt, 4.0) after attempts 1 and 2


async def test_adapter_retries_exhausted_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(openai_compatible.asyncio, "sleep", _fake_sleep)

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"error": "unavailable"})

    async with _adapter(handler, max_retries=1) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["a"])

    assert exc.value.upstream_code == "EMBEDDING_UPSTREAM_ERROR"
    assert len(calls) == 2


async def test_adapter_malformed_response_is_never_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"object": "list", "data": "nope"})

    async with _adapter(handler, max_retries=2) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["a"])

    assert exc.value.upstream_code == "EMBEDDING_MALFORMED_RESPONSE"
    assert len(calls) == 1


async def test_adapter_zero_retries_is_a_single_attempt() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"error": "boom"})

    async with _adapter(handler, max_retries=0) as adapter:
        with pytest.raises(ExternalServiceError):
            await adapter.embed_documents(["a"])

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# OpenAICompatibleEmbeddingAdapter — configuration + secret hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"api_key": ""},
        {"api_key": "   "},
        {"base_url": ""},
        {"base_url": "  "},
        {"model_id": ""},
        {"model_id": "  "},
    ],
)
async def test_adapter_fails_closed_when_not_configured(
    overrides: dict[str, object],
) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=_payload([list(_GOOD_VECTORS[0])]))

    with pytest.raises(ExternalServiceError) as exc:
        async with _adapter(handler, **overrides):
            pass

    assert exc.value.upstream_code == "EMBEDDING_NOT_CONFIGURED"
    assert calls == []


async def test_adapter_error_never_leaks_the_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A hostile upstream echoing the key in its body must not reach the caller.
        return httpx.Response(500, json={"error": f"key {_API_KEY} rejected"})

    async with _adapter(handler, max_retries=0) as adapter:
        with pytest.raises(ExternalServiceError) as exc:
            await adapter.embed_documents(["a"])

    assert _API_KEY not in str(exc.value)
    assert _API_KEY not in repr(exc.value)
    assert _API_KEY not in json.dumps(vars(exc.value), default=repr)


async def test_adapter_aclose_leaves_an_injected_client_open() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_payload([list(_GOOD_VECTORS[0])]))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = _build_adapter(client)
    try:
        await adapter.aclose()
        assert client.is_closed is False  # the caller owns the injected client
    finally:
        await client.aclose()


async def test_adapter_aclose_without_a_client_is_a_noop() -> None:
    adapter = OpenAICompatibleEmbeddingAdapter(
        base_url=_BASE_URL,
        api_key=_API_KEY,
        model_id=_MODEL_ID,
        dimension=_DIMENSION,
    )
    await adapter.aclose()
    assert adapter.descriptor.dimension == _DIMENSION
