"""The cross-service memory invariants okf_core owns: namespace/actor-id
derivation (write and read must never diverge between the chat runtime and
the Control API) and the metadata-vs-fallback provenance flag recall keys off.
Record PARSING is exercised end-to-end in the chat service's tests; this file
pins the shared-contract pieces.
"""

from okf_core.memory_records import (
    memory_namespace,
    parse_record,
    safe_actor_id,
)


def test_safe_actor_id_matches_create_event_pattern():
    # CreateEvent constrains actorId/sessionId to [a-zA-Z0-9][a-zA-Z0-9-_]* —
    # the runtime's internal thread id carries a colon (failed live).
    assert safe_actor_id("sub-123:thread.9") == "sub-123_thread_9"
    assert safe_actor_id("plain-uuid-sub") == "plain-uuid-sub"  # normal no-op
    assert safe_actor_id("_leading") == "m_leading"  # must start alnum
    assert safe_actor_id("") == "m"


def test_memory_namespace_derives_from_the_sanitized_id():
    # The strategy resolves {actorId} from CreateEvent's SANITIZED actorId;
    # a namespace built from the raw sub would list an empty page while the
    # runtime kept writing (the exact drift this module exists to prevent).
    assert memory_namespace("abc-123") == "wiki/abc-123"
    assert memory_namespace("sub:colon") == "wiki/sub_colon"
    assert memory_namespace("sub:colon", prefix="other") == "other/sub_colon"


def test_type_from_metadata_flags_filterable_records():
    real = parse_record(
        {
            "memoryRecordId": "m1",
            "content": {"text": "Role: analyst"},
            "metadata": {"type": {"stringValue": "personal"}},
        }
    )
    assert real["type"] == "personal" and real["type_from_metadata"] is True

    # Header/content-embedded types are display-true but a server-side type
    # metadataFilter can NOT match them — consumers must know the difference.
    header = parse_record(
        {
            "memoryRecordId": "m2",
            "content": {"text": "[type:personal] [dataset:-] [expires:-]\nName: E."},
        }
    )
    assert header["type"] == "personal" and header["type_from_metadata"] is False

    embedded = parse_record(
        {
            "memoryRecordId": "m3",
            "content": {
                "text": '{"context": "c", "preference": "Name: E.",'
                ' "metadata": {"type": "personal"}}'
            },
        }
    )
    assert embedded["type"] == "personal"
    assert embedded["type_from_metadata"] is False
