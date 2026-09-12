"""Sending attachments: client input validation and single-use message binding."""

from __future__ import annotations

import pytest

from core.api.chat import _attachment_ids, _prepare_attachments
from core.services import attachments
from core.state.db import LOCAL_USER_ID, Store


# ------------------------------------------------- client input validation


def test_no_attachment_ids_is_an_empty_list():
    assert _attachment_ids(None) == []
    assert _attachment_ids("") == []
    assert _attachment_ids([]) == []


def test_attachment_ids_are_deduplicated_in_order():
    assert _attachment_ids([3, 1, 3, 2, 1]) == [3, 1, 2]


@pytest.mark.parametrize("bad", ["7", {"id": 7}, 7])
def test_attachment_ids_must_be_a_list(bad):
    with pytest.raises(ValueError, match="list"):
        _attachment_ids(bad)


@pytest.mark.parametrize("bad", [["7"], [None], [1.5], [True]])
def test_each_identifier_must_be_a_plain_integer(bad):
    with pytest.raises(ValueError, match="integer"):
        _attachment_ids(bad)


@pytest.mark.parametrize("bad", [[0], [-1]])
def test_identifiers_must_be_positive(bad):
    with pytest.raises(ValueError, match="positive"):
        _attachment_ids(bad)


def test_a_message_cannot_carry_unlimited_attachments():
    too_many = list(range(1, attachments.MAX_ATTACHMENTS_PER_MESSAGE + 2))
    with pytest.raises(ValueError, match="at most"):
        _attachment_ids(too_many)


# ------------------------------------------------------------ store binding


@pytest.fixture
def store(tmp_path):
    """A fresh store bootstraps its own local owner principal."""
    return Store(tmp_path / "state.db")


def owner_id(store) -> str:
    return LOCAL_USER_ID


def second_user_id(store) -> str:
    return str(store.invite_test_user("tester@example.com")["id"])


def add_upload(store, owner, *, filename="spec.txt", sha=None) -> int:
    return store.add_attachment(
        user_id=owner,
        filename=filename,
        kind="text",
        mime="text/plain",
        extension=".txt",
        sha256=sha or ("a" * 64),
        byte_count=12,
        extraction_method="decoded",
        extracted_chars=12,
    )


def test_an_upload_starts_unbound(store):
    owner = owner_id(store)
    record = store.get_attachment(add_upload(store, owner), user_id=owner)
    assert record["message_id"] is None
    assert record["conversation_id"] is None


def test_binding_attaches_the_upload_to_its_message(store):
    owner = owner_id(store)
    attachment_id = add_upload(store, owner)
    conversation_id = store.create_conversation(user_id=owner)
    message_id = store.add_message(conversation_id, "user", "here you go")

    bound = store.bind_attachments(
        [attachment_id],
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=owner,
    )
    assert [record["id"] for record in bound] == [attachment_id]

    stored = store.get_attachment(attachment_id, user_id=owner)
    assert stored["message_id"] == message_id
    assert stored["conversation_id"] == conversation_id


def test_binding_is_single_use(store):
    """A resent id must not put the same upload into a second message."""
    owner = owner_id(store)
    attachment_id = add_upload(store, owner)
    conversation_id = store.create_conversation(user_id=owner)
    first = store.add_message(conversation_id, "user", "one")
    second = store.add_message(conversation_id, "user", "two")

    store.bind_attachments(
        [attachment_id], conversation_id=conversation_id, message_id=first, user_id=owner
    )
    again = store.bind_attachments(
        [attachment_id], conversation_id=conversation_id, message_id=second, user_id=owner
    )
    assert again == []
    assert store.get_attachment(attachment_id, user_id=owner)["message_id"] == first


def test_binding_refuses_another_users_upload(store):
    owner = owner_id(store)
    other = second_user_id(store)
    attachment_id = add_upload(store, other)

    conversation_id = store.create_conversation(user_id=owner)
    message_id = store.add_message(conversation_id, "user", "mine now")
    assert store.bind_attachments(
        [attachment_id],
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=owner,
    ) == []
    assert store.get_attachment(attachment_id, user_id=other)["message_id"] is None


def test_get_attachment_is_owner_scoped(store):
    owner = owner_id(store)
    attachment_id = add_upload(store, owner)
    assert store.get_attachment(attachment_id, user_id="someone-else") is None
    assert store.get_attachment(attachment_id, user_id=owner) is not None


def test_conversation_attachments_are_listed_in_order(store):
    owner = owner_id(store)
    conversation_id = store.create_conversation(user_id=owner)
    message_id = store.add_message(conversation_id, "user", "two files")
    first = add_upload(store, owner, filename="a.txt", sha="a" * 64)
    second = add_upload(store, owner, filename="b.txt", sha="b" * 64)
    store.bind_attachments(
        [first, second],
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=owner,
    )
    listed = store.list_conversation_attachments(conversation_id)
    assert [record["filename"] for record in listed] == ["a.txt", "b.txt"]


def test_abandoned_uploads_are_swept_but_sent_ones_are_kept(store):
    owner = owner_id(store)
    conversation_id = store.create_conversation(user_id=owner)
    message_id = store.add_message(conversation_id, "user", "sent")
    sent = add_upload(store, owner, filename="sent.txt", sha="c" * 64)
    store.bind_attachments(
        [sent], conversation_id=conversation_id, message_id=message_id, user_id=owner
    )
    abandoned = add_upload(store, owner, filename="abandoned.txt", sha="d" * 64)
    store._exec(
        "UPDATE attachments SET created_at = datetime('now','-3 days') WHERE id = ?",
        (abandoned,),
    )

    swept = store.delete_unbound_attachments(older_than_hours=24)
    assert [record["id"] for record in swept] == [abandoned]
    assert store.get_attachment(abandoned, user_id=owner) is None
    assert store.get_attachment(sent, user_id=owner) is not None


def test_shared_bytes_are_detected_before_deletion(store):
    owner = owner_id(store)
    first = add_upload(store, owner, filename="one.txt", sha="e" * 64)
    second = add_upload(store, owner, filename="two.txt", sha="e" * 64)
    assert store.attachment_sha_in_use("e" * 64, excluding_id=first) is True
    store._exec("DELETE FROM attachments WHERE id = ?", (second,))
    assert store.attachment_sha_in_use("e" * 64, excluding_id=first) is False


# --------------------------------------------------------- send preparation


def test_prepare_builds_a_block_per_attachment(store, tmp_path):
    owner = owner_id(store)
    accepted = attachments.accept(b"torque 9 Nm", "spec.txt")
    extracted = attachments.extract(accepted)
    attachments.write_attachment(tmp_path, accepted, extracted)
    attachment_id = store.add_attachment(
        user_id=owner,
        filename=accepted.filename,
        kind=accepted.kind,
        mime=accepted.mime,
        extension=accepted.extension,
        sha256=accepted.sha256,
        byte_count=accepted.byte_count,
        extraction_method=extracted.method,
        extracted_chars=len(extracted.text),
    )

    prepared = _prepare_attachments(store, [attachment_id], owner, tmp_path)
    assert len(prepared) == 1
    record, block = prepared[0]
    assert record["id"] == attachment_id
    assert "spec.txt" in block
    assert "torque 9 Nm" in block


def test_prepare_refuses_an_unknown_attachment(store, tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        _prepare_attachments(store, [4242], owner_id(store), tmp_path)


def test_prepare_refuses_another_users_attachment(store, tmp_path):
    attachment_id = add_upload(store, second_user_id(store))
    with pytest.raises(ValueError, match="does not exist"):
        _prepare_attachments(store, [attachment_id], owner_id(store), tmp_path)


def test_prepare_refuses_an_already_sent_attachment(store, tmp_path):
    owner = owner_id(store)
    attachment_id = add_upload(store, owner)
    conversation_id = store.create_conversation(user_id=owner)
    message_id = store.add_message(conversation_id, "user", "first")
    store.bind_attachments(
        [attachment_id],
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=owner,
    )
    with pytest.raises(ValueError, match="already sent"):
        _prepare_attachments(store, [attachment_id], owner, tmp_path)
