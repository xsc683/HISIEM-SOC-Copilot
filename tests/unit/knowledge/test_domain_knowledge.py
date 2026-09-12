"""Domain-only tests for the P3-A knowledge aggregate and its value objects.

No mocks, no I/O: every test drives the aggregate directly and pins the
invariants the rest of the knowledge pipeline depends on -- scope is a check
constraint, bounds are rejections, content normalization is byte-faithful for
security identifiers, and the content hash is platform-independent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from hisiem_soc_copilot.domain.knowledge.entities import (
    MAX_EXTERNAL_KEY_CHARS,
    MAX_LANGUAGE_CHARS,
    MAX_SOURCE_VERSION_CHARS,
    MAX_TITLE_CHARS,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
)
from hisiem_soc_copilot.domain.knowledge.enums import (
    DocumentStatus,
    SourceKind,
    Visibility,
)
from hisiem_soc_copilot.domain.knowledge.errors import (
    InvalidContentHashError,
    InvalidKnowledgeDocumentError,
    InvalidKnowledgeScopeError,
    InvalidKnowledgeVersionError,
    KnowledgeDocumentStateError,
)
from hisiem_soc_copilot.domain.knowledge.value_objects import (
    CHUNKER_VERSION,
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_TARGET_TOKENS,
    ChunkerProfile,
    compute_content_hash,
    is_valid_content_hash,
    normalize_and_hash,
    normalize_knowledge_content,
    require_valid_content_hash,
)

DOCUMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
VERSION_ID = UUID("22222222-2222-4222-8222-222222222222")

TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"
T0 = datetime(2026, 9, 12, 9, 0, 0, tzinfo=UTC)

#: A valid lowercase SHA-256 hex digest, used wherever the hash value itself is
#: irrelevant to the assertion.
VALID_HASH = "0f" * 32

#: The security identifiers that normalization must never touch.
SECURITY_IDENTIFIERS = ("T1110", "sshd", "authentication_failure", "CVE-2024-3094")

BODY = "T1110 brute force guidance\n"


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _create_document(
    *,
    visibility: Visibility = Visibility.GLOBAL,
    tenant_id: str | None = None,
    title: str = "Brute Force Guidance",
    external_key: str = "guidance-brute-force",
    source_kind: SourceKind = SourceKind.CURATED_GUIDANCE,
) -> KnowledgeDocument:
    return KnowledgeDocument.create(
        id=DOCUMENT_ID,
        source_kind=source_kind,
        external_key=external_key,
        visibility=visibility,
        tenant_id=tenant_id,
        title=title,
        now=T0,
    )


def _create_version(
    *,
    version: int = 1,
    content: str = BODY,
    content_hash: str | None = None,
    title: str = "Brute Force Guidance",
    language: str = "en",
    source_version: str | None = "2026.09",
) -> KnowledgeDocumentVersion:
    return KnowledgeDocumentVersion.create(
        id=VERSION_ID,
        document_id=DOCUMENT_ID,
        version=version,
        normalized_content=content,
        content_hash=content_hash if content_hash is not None else compute_content_hash(content),
        title=title,
        language=language,
        source_version=source_version,
        ingested_at=T0,
    )


# ---------------------------------------------------------------------------
# KnowledgeDocument.create -- identity, scope, bounds
# ---------------------------------------------------------------------------


def test_document_create_emits_exactly_one_created_event() -> None:
    document = _create_document()

    events = document.pending_events
    assert len(events) == 1
    event = events[0]
    assert event.event_type == "knowledge_document_created"
    assert event.aggregate_type == "knowledge_document"
    assert event.aggregate_id == DOCUMENT_ID
    assert event.occurred_at is not None
    assert event.payload == {
        "source_kind": "CURATED_GUIDANCE",
        "external_key": "guidance-brute-force",
        "visibility": "GLOBAL",
    }
    assert event.tenant_id is None


def test_document_create_records_the_tenant_on_a_tenant_scoped_document() -> None:
    document = _create_document(visibility=Visibility.TENANT, tenant_id=TENANT)

    assert document.tenant_id == TENANT
    assert document.pending_events[0].tenant_id == TENANT
    assert document.pending_events[0].payload["visibility"] == "TENANT"


def test_pending_events_returns_a_copy() -> None:
    document = _create_document()

    handed_out = document.pending_events
    handed_out.clear()
    handed_out.append(document.pending_events[0])

    assert len(document.pending_events) == 1
    assert document.pending_events is not handed_out
    assert document.pending_events[0].event_type == "knowledge_document_created"


def test_clear_events_discards_the_pending_ledger() -> None:
    document = _create_document()

    document.clear_events()

    assert document.pending_events == []


@pytest.mark.parametrize("title", ["", " ", "\t\n  "])
def test_document_create_rejects_an_empty_title(title: str) -> None:
    with pytest.raises(InvalidKnowledgeDocumentError) as excinfo:
        _create_document(title=title)

    assert excinfo.value.code == "INVALID_KNOWLEDGE_DOCUMENT"


def test_document_create_accepts_a_title_at_the_limit_and_rejects_one_over_it() -> None:
    assert _create_document(title="t" * MAX_TITLE_CHARS).title == "t" * MAX_TITLE_CHARS

    with pytest.raises(InvalidKnowledgeDocumentError) as excinfo:
        _create_document(title="t" * (MAX_TITLE_CHARS + 1))

    assert excinfo.value.code == "INVALID_KNOWLEDGE_DOCUMENT"
    assert str(MAX_TITLE_CHARS) in str(excinfo.value)


def test_document_create_accepts_an_external_key_at_the_limit_and_rejects_one_over() -> None:
    accepted = _create_document(external_key="k" * MAX_EXTERNAL_KEY_CHARS)
    assert accepted.external_key == "k" * MAX_EXTERNAL_KEY_CHARS

    with pytest.raises(InvalidKnowledgeDocumentError) as excinfo:
        _create_document(external_key="k" * (MAX_EXTERNAL_KEY_CHARS + 1))

    assert excinfo.value.code == "INVALID_KNOWLEDGE_DOCUMENT"


def test_document_create_rejects_an_empty_external_key() -> None:
    with pytest.raises(InvalidKnowledgeDocumentError) as excinfo:
        _create_document(external_key="  ")

    assert excinfo.value.code == "INVALID_KNOWLEDGE_DOCUMENT"


def test_global_document_with_a_tenant_is_rejected() -> None:
    with pytest.raises(InvalidKnowledgeScopeError) as excinfo:
        _create_document(visibility=Visibility.GLOBAL, tenant_id=TENANT)

    assert excinfo.value.code == "INVALID_KNOWLEDGE_SCOPE"


@pytest.mark.parametrize("tenant_id", [None, "", "   "])
def test_tenant_document_without_a_tenant_is_rejected(tenant_id: str | None) -> None:
    with pytest.raises(InvalidKnowledgeScopeError) as excinfo:
        _create_document(visibility=Visibility.TENANT, tenant_id=tenant_id)

    assert excinfo.value.code == "INVALID_KNOWLEDGE_SCOPE"


# ---------------------------------------------------------------------------
# KnowledgeDocument -- lifecycle
# ---------------------------------------------------------------------------


def test_activate_version_bumps_revision_updates_title_and_emits_an_event() -> None:
    document = _create_document()
    document.clear_events()

    document.activate_version(
        version_id=VERSION_ID,
        version=1,
        content_hash=VALID_HASH,
        title="Brute Force Guidance (rev 2)",
    )

    assert document.active_version_id == VERSION_ID
    assert document.title == "Brute Force Guidance (rev 2)"
    assert document.revision == 1
    assert document.status is DocumentStatus.ACTIVE

    events = document.pending_events
    assert [event.event_type for event in events] == ["knowledge_document_version_ingested"]
    assert events[0].aggregate_id == DOCUMENT_ID
    assert events[0].payload == {
        "document_version_id": str(VERSION_ID),
        "version": 1,
        "content_hash": VALID_HASH,
    }


def test_reactivating_the_active_version_is_a_no_op_that_still_audits() -> None:
    document = _create_document()
    document.activate_version(
        version_id=VERSION_ID, version=1, content_hash=VALID_HASH, title="same"
    )
    document.clear_events()

    document.activate_version(
        version_id=VERSION_ID, version=1, content_hash=VALID_HASH, title="same"
    )

    assert document.revision == 2
    assert [event.event_type for event in document.pending_events] == [
        "knowledge_document_version_ingested"
    ]


def test_retire_sets_status_and_timestamp_and_emits_an_event() -> None:
    document = _create_document()
    document.clear_events()
    retired_at = T0 + timedelta(days=30)

    document.retire(now=retired_at)

    assert document.status is DocumentStatus.RETIRED
    assert document.retired_at == retired_at
    assert document.revision == 1
    assert document.is_active() is False
    assert [event.event_type for event in document.pending_events] == [
        "knowledge_document_retired"
    ]
    assert document.pending_events[0].aggregate_id == DOCUMENT_ID


def test_retirement_is_terminal() -> None:
    document = _create_document()
    document.retire(now=T0 + timedelta(days=1))
    document.clear_events()

    with pytest.raises(KnowledgeDocumentStateError) as excinfo:
        document.retire(now=T0 + timedelta(days=2))

    assert excinfo.value.code == "INVALID_STATE_TRANSITION"
    assert excinfo.value.details == {
        "aggregate_type": "knowledge_document",
        "current_status": "RETIRED",
        "command": "retire",
    }
    # RETIRED never returns to ACTIVE and the second retire changed nothing.
    assert document.status is DocumentStatus.RETIRED
    assert document.retired_at == T0 + timedelta(days=1)
    assert document.revision == 1
    assert document.pending_events == []


def test_activate_version_on_a_retired_document_is_rejected() -> None:
    document = _create_document()
    document.retire(now=T0 + timedelta(days=1))
    document.clear_events()

    with pytest.raises(KnowledgeDocumentStateError) as excinfo:
        document.activate_version(
            version_id=VERSION_ID, version=2, content_hash=VALID_HASH, title="nope"
        )

    assert excinfo.value.code == "INVALID_STATE_TRANSITION"
    assert excinfo.value.details["command"] == "activate_version"
    assert excinfo.value.details["current_status"] == "RETIRED"
    assert document.active_version_id is None
    assert document.pending_events == []


# ---------------------------------------------------------------------------
# KnowledgeDocument -- visibility
# ---------------------------------------------------------------------------


def test_a_global_document_is_visible_to_every_tenant() -> None:
    document = _create_document(visibility=Visibility.GLOBAL)

    assert document.is_visible_to(TENANT) is True
    assert document.is_visible_to(OTHER_TENANT) is True


def test_a_tenant_document_is_visible_only_to_its_owner() -> None:
    document = _create_document(visibility=Visibility.TENANT, tenant_id=TENANT)

    assert document.is_visible_to(TENANT) is True
    assert document.is_visible_to(OTHER_TENANT) is False


# ---------------------------------------------------------------------------
# KnowledgeDocumentVersion.create -- immutability invariants
# ---------------------------------------------------------------------------


def test_version_create_accepts_version_one_with_a_valid_hash() -> None:
    version = _create_version()

    assert version.version == 1
    assert version.document_id == DOCUMENT_ID
    assert version.content_hash == compute_content_hash(BODY)
    assert version.normalized_content == BODY
    assert version.language == "en"
    assert version.source_version == "2026.09"
    assert version.ingested_at == T0
    assert is_valid_content_hash(version.content_hash)


@pytest.mark.parametrize("version_number", [0, -1, -100])
def test_version_create_rejects_non_positive_versions(version_number: int) -> None:
    with pytest.raises(InvalidKnowledgeVersionError) as excinfo:
        _create_version(version=version_number)

    assert excinfo.value.code == "INVALID_KNOWLEDGE_VERSION"


@pytest.mark.parametrize(
    "content_hash",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,  # uppercase is a different digest, not a decoration
        "g" * 64,  # not hex
        "0f" * 32 + "\n",
        "sha256:" + "0f" * 32,
    ],
)
def test_version_create_rejects_a_malformed_content_hash(content_hash: str) -> None:
    with pytest.raises(InvalidKnowledgeVersionError) as excinfo:
        _create_version(content_hash=content_hash)

    assert excinfo.value.code == "INVALID_KNOWLEDGE_VERSION"


def test_version_create_rejects_empty_content() -> None:
    with pytest.raises(InvalidKnowledgeVersionError) as excinfo:
        _create_version(content="")

    assert excinfo.value.code == "INVALID_KNOWLEDGE_VERSION"


@pytest.mark.parametrize("content", [" ", "\n", "\t \n\t"])
def test_version_create_rejects_whitespace_only_content(content: str) -> None:
    with pytest.raises(InvalidKnowledgeVersionError) as excinfo:
        _create_version(content=content)

    assert excinfo.value.code == "INVALID_KNOWLEDGE_VERSION"


def test_version_create_rejects_content_that_is_not_already_normalized() -> None:
    """Un-normalized bytes are a caller bug, not something the domain repairs.

    The guard lives on ``__post_init__``, so it holds for every construction
    path; ``create`` is simply the ergonomic way in.
    """
    with pytest.raises(InvalidKnowledgeVersionError) as excinfo:
        _create_version(content="line one\r\nline two")

    assert excinfo.value.code == "INVALID_KNOWLEDGE_VERSION"
    assert str(excinfo.value) == "normalized_content must already be normalized"


def test_version_constructor_also_rejects_un_normalized_content() -> None:
    """The invariant holds on EVERY construction path, not just ``create``.

    ``KnowledgeDocumentVersion`` is also built directly from persisted rows by
    the mapper and by tests. If only ``create`` checked, a version carrying
    non-normalized "normalized" content would carry a hash that re-normalizing
    the same bytes could not reproduce -- provenance silently broken on one
    path.
    """
    with pytest.raises(InvalidKnowledgeVersionError) as excinfo:
        KnowledgeDocumentVersion(
            id=VERSION_ID,
            document_id=DOCUMENT_ID,
            version=1,
            content_hash=VALID_HASH,
            title="Brute Force Guidance",
            normalized_content="line one\r\nline two",
            ingested_at=T0,
        )

    assert str(excinfo.value) == "normalized_content must already be normalized"


def _create_version_with(field_name: str, value: str) -> KnowledgeDocumentVersion:
    """Build a version with one over-long text field set."""
    if field_name == "title":
        return _create_version(title=value)
    if field_name == "language":
        return _create_version(language=value)
    if field_name == "source_version":
        return _create_version(source_version=value)
    raise AssertionError(f"unknown field {field_name!r}")


@pytest.mark.parametrize(
    ("field_name", "value", "limit"),
    [
        ("title", "t" * (MAX_TITLE_CHARS + 1), MAX_TITLE_CHARS),
        ("language", "l" * (MAX_LANGUAGE_CHARS + 1), MAX_LANGUAGE_CHARS),
        ("source_version", "s" * (MAX_SOURCE_VERSION_CHARS + 1), MAX_SOURCE_VERSION_CHARS),
    ],
)
def test_version_create_rejects_over_long_text_fields(
    field_name: str, value: str, limit: int
) -> None:
    with pytest.raises(InvalidKnowledgeVersionError) as excinfo:
        _create_version_with(field_name, value)

    assert excinfo.value.code == "INVALID_KNOWLEDGE_VERSION"
    assert str(limit) in str(excinfo.value)


def test_version_create_accepts_text_fields_at_their_limits() -> None:
    version = _create_version(
        title="t" * MAX_TITLE_CHARS,
        language="l" * MAX_LANGUAGE_CHARS,
        source_version="s" * MAX_SOURCE_VERSION_CHARS,
    )

    assert len(version.title) == MAX_TITLE_CHARS
    assert len(version.language) == MAX_LANGUAGE_CHARS
    assert version.source_version is not None
    assert len(version.source_version) == MAX_SOURCE_VERSION_CHARS


def test_version_create_rejects_an_empty_title() -> None:
    with pytest.raises(InvalidKnowledgeVersionError) as excinfo:
        _create_version(title="   ")

    assert excinfo.value.code == "INVALID_KNOWLEDGE_VERSION"


# ---------------------------------------------------------------------------
# normalize_knowledge_content
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["line one\r\nline two\r\n", "line one\rline two\r", "line one\nline two\n"],
)
def test_line_endings_all_collapse_to_lf(raw: str) -> None:
    assert normalize_knowledge_content(raw) == "line one\nline two\n"


def test_trailing_whitespace_is_trimmed_on_every_line() -> None:
    assert normalize_knowledge_content("alpha   \nbeta\t\n") == "alpha\nbeta\n"


@pytest.mark.parametrize("raw", ["body", "body\n", "body\n\n\n", "body  \n\t\n\n"])
def test_output_ends_with_exactly_one_newline(raw: str) -> None:
    assert normalize_knowledge_content(raw) == "body\n"


@pytest.mark.parametrize("raw", ["", " ", "\n", "\n\n\n", " \t\n  \n\t"])
def test_empty_and_whitespace_only_content_normalizes_to_the_empty_string(raw: str) -> None:
    assert normalize_knowledge_content(raw) == ""


def test_unicode_is_nfc_normalized() -> None:
    decomposed = "café password\n"

    assert normalize_knowledge_content(decomposed) == "café password\n"


def test_a_leading_bom_is_stripped() -> None:
    assert normalize_knowledge_content("﻿alpha\n") == "alpha\n"


@pytest.mark.parametrize("identifier", SECURITY_IDENTIFIERS)
def test_security_identifiers_survive_byte_identically(identifier: str) -> None:
    raw = f"{identifier}\n"

    # Already-normalized text round-trips unchanged: no lower-casing, no
    # punctuation stripping, no stemming.
    assert normalize_knowledge_content(raw) == raw
    assert identifier in normalize_knowledge_content(f"Rule {identifier} matched the log.\n")
    if identifier != identifier.lower():
        assert identifier.lower() not in normalize_knowledge_content(raw)


def test_a_security_body_keeps_every_identifier_exactly() -> None:
    raw = (
        "# Authentication Failures\r\n\r\n"
        "Rule T1110 matched on the sshd attribute authentication_failure.   \r\n"
        "See CVE-2024-3094 for the upstream issue.\r\n"
    )

    normalized = normalize_knowledge_content(raw)

    for identifier in SECURITY_IDENTIFIERS:
        assert identifier in normalized
    assert "\r" not in normalized
    assert normalized.endswith("\n")
    assert not normalized.endswith("\n\n")


def test_normalization_is_idempotent() -> None:
    raw = "  Alpha \r\n\r\n beta\t\n\n\n"

    once = normalize_knowledge_content(raw)

    assert normalize_knowledge_content(once) == once


# ---------------------------------------------------------------------------
# compute_content_hash / require_valid_content_hash
# ---------------------------------------------------------------------------


def test_windows_and_unix_line_endings_hash_identically() -> None:
    unix = "T1110 brute force guidance\nsecond line\n"
    windows = "T1110 brute force guidance\r\nsecond line\r\n"
    normalized_unix, hash_unix = normalize_and_hash(unix)
    normalized_windows, hash_windows = normalize_and_hash(windows)

    assert normalized_unix == normalized_windows
    assert hash_unix == hash_windows
    # The property is not vacuous: the raw bytes really do differ.
    assert unix.encode("utf-8") != windows.encode("utf-8")
    assert compute_content_hash(windows) != hash_windows


def test_content_hash_is_a_lowercase_sha256_hex_digest() -> None:
    digest = compute_content_hash(BODY)

    assert len(digest) == 64
    assert digest == digest.lower()
    assert is_valid_content_hash(digest)
    assert compute_content_hash(BODY) == digest


def test_different_content_hashes_differently() -> None:
    assert compute_content_hash("alpha\n") != compute_content_hash("beta\n")


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        "abc",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        "sha256:" + "0f" * 32,
        "0f" * 32 + " ",
    ],
)
def test_require_valid_content_hash_rejects_anything_but_sha256(candidate: str) -> None:
    with pytest.raises(InvalidContentHashError) as excinfo:
        require_valid_content_hash(candidate)

    assert excinfo.value.code == "INVALID_CONTENT_HASH"
    assert is_valid_content_hash(candidate) is False


def test_require_valid_content_hash_returns_a_valid_digest() -> None:
    assert require_valid_content_hash(VALID_HASH) == VALID_HASH


# ---------------------------------------------------------------------------
# ChunkerProfile
# ---------------------------------------------------------------------------


def test_chunker_profile_defaults_are_frozen() -> None:
    profile = ChunkerProfile()

    assert profile.chunker_version == CHUNKER_VERSION
    assert profile.target_tokens == DEFAULT_TARGET_TOKENS
    assert profile.max_tokens == DEFAULT_MAX_TOKENS
    assert profile.overlap_tokens == DEFAULT_OVERLAP_TOKENS


def test_chunker_profile_accepts_the_ceiling() -> None:
    profile = ChunkerProfile(target_tokens=800, max_tokens=800, overlap_tokens=0)

    assert profile.max_tokens == 800


@pytest.mark.parametrize(
    ("target_tokens", "max_tokens", "overlap_tokens"),
    [
        (100, 50, 0),  # max_tokens below target_tokens
        (600, 801, 0),  # max_tokens over the 800 ceiling
        (100, 100, 100),  # overlap equal to target_tokens
        (100, 100, 150),  # overlap over target_tokens
        (100, 100, -1),  # negative overlap
        (0, 0, 0),  # target_tokens below 1
    ],
)
def test_chunker_profile_rejects_invalid_bounds(
    target_tokens: int, max_tokens: int, overlap_tokens: int
) -> None:
    with pytest.raises(InvalidKnowledgeDocumentError) as excinfo:
        ChunkerProfile(
            target_tokens=target_tokens,
            max_tokens=max_tokens,
            overlap_tokens=overlap_tokens,
        )

    assert excinfo.value.code == "INVALID_KNOWLEDGE_DOCUMENT"


def test_chunker_profile_rejects_an_empty_version() -> None:
    with pytest.raises(InvalidKnowledgeDocumentError) as excinfo:
        ChunkerProfile(chunker_version="  ")

    assert excinfo.value.code == "INVALID_KNOWLEDGE_DOCUMENT"
