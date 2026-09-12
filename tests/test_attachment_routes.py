"""Attachment upload/serve routes and conversation transcript export."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from core.api.routes import create_router
from core.state.db import LOCAL_USER_ID, Store

ORIGIN = "http://127.0.0.1:8100"


class _Router:
    """A worker that can read images without needing a swap."""

    active_name = "omni"

    def supports_vision(self):
        return True


class _Registry:
    policy = {}
    roots = []
    _handlers = {}

    @staticmethod
    def tier(_name):
        return "blocked"

    @staticmethod
    def public_approval(record, receipt=None):
        return record


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        local_origin=ORIGIN,
        public_origin="",
        vapid_public_key="k",
        attachment_dir=tmp_path / "attachments",
        camera_snapshot_dir=tmp_path / "snapshots",
    )


def _app(store: Store, settings: SimpleNamespace, *, role: str = "owner") -> FastAPI:
    async def session():
        return {"google_sub": "owner", "user_id": LOCAL_USER_ID, "role": role}

    app = FastAPI()
    app.include_router(create_router(settings, store, _Router(), _Registry(), session))
    return app


@pytest.fixture
def context(tmp_path):
    settings = _settings(tmp_path)
    store = Store(tmp_path / "routes.sqlite")
    return SimpleNamespace(
        settings=settings, store=store, client=TestClient(_app(store, settings))
    )


def upload(client, name: str, payload: bytes, content_type: str = "text/plain"):
    return client.post(
        "/api/attachments",
        files={"file": (name, io.BytesIO(payload), content_type)},
        headers={"Origin": ORIGIN},
    )


# ------------------------------------------------------------------- upload


def test_uploading_a_text_file_reads_it_immediately(context):
    response = upload(context.client, "spec.txt", b"Torque 9 Nm\nTarget 1.5 m\n")
    assert response.status_code == 200

    body = response.json()
    assert body["ok"] is True
    record = body["attachment"]
    assert record["filename"] == "spec.txt"
    assert record["kind"] == "text"
    assert record["extraction_method"] == "decoded"
    assert record["truncated"] is False
    # The operator sees what X made of the file before the message is sent.
    assert "Torque 9 Nm" in body["preview"]


def test_an_upload_is_persisted_and_content_addressed(context):
    payload = b"stored bytes"
    record = upload(context.client, "notes.txt", payload).json()["attachment"]

    stored = context.settings.attachment_dir / f"{record['sha256']}.txt"
    sidecar = context.settings.attachment_dir / f"{record['sha256']}.extracted.txt"
    assert stored.read_bytes() == payload
    assert sidecar.read_text(encoding="utf-8") == "stored bytes"

    row = context.store.get_attachment(record["id"], user_id=LOCAL_USER_ID)
    assert row["sha256"] == record["sha256"]
    assert row["message_id"] is None


def test_a_docx_upload_is_read_as_a_word_document(context):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Front radar calibration procedure")
    buffer = io.BytesIO()
    document.save(buffer)

    response = upload(
        context.client,
        "procedure.docx",
        buffer.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["attachment"]["kind"] == "docx"
    assert "Front radar calibration procedure" in body["preview"]


def test_an_unreadable_file_type_is_refused_with_a_usable_message(context):
    response = upload(context.client, "firmware.bin", b"\x00\x01\x02\x03\x00", "application/octet-stream")
    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


def test_an_empty_upload_is_refused(context):
    assert upload(context.client, "empty.txt", b"").status_code == 400


def test_uploads_must_come_from_the_x_omni_origin(context):
    response = context.client.post(
        "/api/attachments",
        files={"file": ("spec.txt", io.BytesIO(b"hello"), "text/plain")},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_a_filename_cannot_carry_a_path(context):
    record = upload(context.client, "../../etc/passwd", b"root:x:0:0").json()["attachment"]
    assert record["filename"] == "passwd"
    # Storage is named by digest, so nothing was written outside the directory.
    written = list(context.settings.attachment_dir.iterdir())
    assert all(path.parent == context.settings.attachment_dir for path in written)


# ------------------------------------------------------------------- serving


def test_the_original_bytes_are_served_back(context):
    payload = b"the original file contents"
    record = upload(context.client, "orig.txt", payload).json()["attachment"]

    response = context.client.get(f"/api/attachments/{record['id']}")
    assert response.status_code == 200
    assert response.content == payload
    assert "inline" in response.headers["content-disposition"]

    download = context.client.get(f"/api/attachments/{record['id']}?download=true")
    assert "attachment" in download.headers["content-disposition"]


def test_an_image_upload_round_trips_its_bytes(context):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (12, 80, 160)).save(buffer, format="PNG")
    payload = buffer.getvalue()

    # The fake router reports vision support; captioning is the model's job and
    # is covered by the service tests, so this only checks storage and serving.
    record = context.store.add_attachment(
        user_id=LOCAL_USER_ID,
        filename="photo.png",
        kind="image",
        mime="image/png",
        extension=".png",
        sha256="f" * 64,
        byte_count=len(payload),
        extraction_method="vision",
    )
    context.settings.attachment_dir.mkdir(parents=True, exist_ok=True)
    (context.settings.attachment_dir / f"{'f' * 64}.png").write_bytes(payload)

    response = context.client.get(f"/api/attachments/{record}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == payload


def test_an_unknown_attachment_is_not_found(context):
    assert context.client.get("/api/attachments/9999").status_code == 404


def test_a_missing_stored_file_is_reported_not_silently_empty(context):
    record = upload(context.client, "gone.txt", b"temporary").json()["attachment"]
    (context.settings.attachment_dir / f"{record['sha256']}.txt").unlink()

    response = context.client.get(f"/api/attachments/{record['id']}")
    assert response.status_code == 404
    assert "missing" in response.json()["detail"].lower()


def test_extracted_text_can_be_paged_through(context):
    body = "".join(f"[{index:04d}]" for index in range(3_000))
    record = upload(context.client, "long.txt", body.encode("utf-8")).json()["attachment"]

    first = context.client.get(f"/api/attachments/{record['id']}/text").json()
    assert first["offset"] == 0
    assert first["has_more"] is True
    assert first["total_chars"] == len(body)

    second = context.client.get(
        f"/api/attachments/{record['id']}/text?offset={first['returned_chars']}"
    ).json()
    assert second["offset"] == first["returned_chars"]
    assert second["text"] != first["text"]


# -------------------------------------------------------------------- export


def _conversation(store: Store) -> int:
    conversation_id = store.create_conversation(user_id=LOCAL_USER_ID)
    store.add_message(conversation_id, "user", "What is the target distance?")
    message_id = store.add_message(
        conversation_id, "assistant", "1.5 metres.", worker_used="omni"
    )
    store.log_tool_call(
        message_id,
        "query_ciq",
        {"ro": "12345"},
        {"ok": True},
        conversation_id=conversation_id,
    )
    return conversation_id


def test_markdown_export_carries_the_whole_conversation(context):
    conversation_id = _conversation(context.store)
    response = context.client.get(f"/api/conversations/{conversation_id}/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment" in response.headers["content-disposition"]
    assert ".md" in response.headers["content-disposition"]

    body = response.text
    assert "What is the target distance?" in body
    assert "1.5 metres." in body
    assert "query_ciq" in body


def test_json_export_is_machine_readable(context):
    conversation_id = _conversation(context.store)
    response = context.client.get(
        f"/api/conversations/{conversation_id}/export?format=json"
    )
    assert response.status_code == 200

    payload = json.loads(response.text)
    assert payload["conversation"]["id"] == conversation_id
    assert len(payload["messages"]) == 2
    assert payload["tool_calls"][0]["tool_name"] == "query_ciq"


def test_export_can_be_read_inline_for_copying(context):
    conversation_id = _conversation(context.store)
    response = context.client.get(
        f"/api/conversations/{conversation_id}/export?download=false"
    )
    assert "inline" in response.headers["content-disposition"]


def test_export_lists_the_files_that_were_attached(context):
    conversation_id = context.store.create_conversation(user_id=LOCAL_USER_ID)
    record = upload(context.client, "spec.txt", b"Torque 9 Nm").json()["attachment"]
    message_id = context.store.add_message(conversation_id, "user", "see attached")
    context.store.bind_attachments(
        [record["id"]],
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=LOCAL_USER_ID,
    )

    body = context.client.get(f"/api/conversations/{conversation_id}/export").text
    assert "Files attached in this conversation" in body
    assert "spec.txt" in body


# --------------------------------------------------- abandoned upload sweep


def _age(store: Store, attachment_id: int, hours: int) -> None:
    store._exec(
        "UPDATE attachments SET created_at = datetime('now', ?) WHERE id = ?",
        (f"-{hours} hours", attachment_id),
    )


def test_the_sweep_removes_uploads_that_were_never_sent(context):
    from core.main import _sweep_abandoned_attachments

    record = upload(context.client, "abandoned.txt", b"never sent").json()["attachment"]
    _age(context.store, record["id"], 48)
    blob = context.settings.attachment_dir / f"{record['sha256']}.txt"
    sidecar = context.settings.attachment_dir / f"{record['sha256']}.extracted.txt"
    assert blob.exists() and sidecar.exists()

    assert _sweep_abandoned_attachments(context.settings, context.store) == 1
    assert context.store.get_attachment(record["id"], user_id=LOCAL_USER_ID) is None
    assert not blob.exists()
    assert not sidecar.exists()


def test_the_sweep_keeps_a_sent_attachment(context):
    from core.main import _sweep_abandoned_attachments

    record = upload(context.client, "sent.txt", b"actually sent").json()["attachment"]
    conversation_id = context.store.create_conversation(user_id=LOCAL_USER_ID)
    message_id = context.store.add_message(conversation_id, "user", "here")
    context.store.bind_attachments(
        [record["id"]],
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=LOCAL_USER_ID,
    )
    _age(context.store, record["id"], 48)

    assert _sweep_abandoned_attachments(context.settings, context.store) == 0
    assert (context.settings.attachment_dir / f"{record['sha256']}.txt").exists()


def test_the_sweep_keeps_bytes_another_attachment_still_shares(context):
    """Content-addressed storage: identical files share one blob on disk."""
    from core.main import _sweep_abandoned_attachments

    payload = b"the very same bytes"
    kept = upload(context.client, "kept.txt", payload).json()["attachment"]
    abandoned = upload(context.client, "abandoned.txt", payload).json()["attachment"]
    assert kept["sha256"] == abandoned["sha256"]

    conversation_id = context.store.create_conversation(user_id=LOCAL_USER_ID)
    message_id = context.store.add_message(conversation_id, "user", "here")
    context.store.bind_attachments(
        [kept["id"]],
        conversation_id=conversation_id,
        message_id=message_id,
        user_id=LOCAL_USER_ID,
    )
    _age(context.store, abandoned["id"], 48)

    assert _sweep_abandoned_attachments(context.settings, context.store) == 1
    # The abandoned row is gone, but the message that was actually sent must
    # still be able to serve its file.
    assert (context.settings.attachment_dir / f"{kept['sha256']}.txt").exists()
    assert context.client.get(f"/api/attachments/{kept['id']}").content == payload


def test_a_recent_unsent_upload_is_left_alone(context):
    from core.main import _sweep_abandoned_attachments

    record = upload(context.client, "still-composing.txt", b"drafting").json()["attachment"]
    assert _sweep_abandoned_attachments(context.settings, context.store) == 0
    assert context.store.get_attachment(record["id"], user_id=LOCAL_USER_ID) is not None


def test_an_unknown_export_format_is_refused(context):
    conversation_id = _conversation(context.store)
    response = context.client.get(
        f"/api/conversations/{conversation_id}/export?format=pdf"
    )
    assert response.status_code == 400


def test_exporting_a_conversation_that_does_not_exist_is_not_found(context):
    assert context.client.get("/api/conversations/4242/export").status_code == 404
