"""Operator chat attachments: detection, extraction, bounding, and binding."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from PIL import Image

from core.services import attachments


# ------------------------------------------------------------------ helpers


def png_bytes(width: int = 40, height: int = 30, colour=(20, 90, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(width: int = 40, height: int = 30) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 40, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


def docx_bytes(paragraphs: list[str], table: list[list[str]] | None = None) -> bytes:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for text in paragraphs:
        document.add_paragraph(text)
    if table:
        created = document.add_table(rows=len(table), cols=len(table[0]))
        for row_index, row in enumerate(table):
            for cell_index, value in enumerate(row):
                created.cell(row_index, cell_index).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets.items():
        sheet = workbook.create_sheet(title=title)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def pdf_bytes(pages: list[str]) -> bytes:
    """A minimal, valid multi-page PDF carrying real embedded text.

    Hand-built rather than pulled from a PDF authoring library: the point is
    to exercise the extractor, and adding a dependency only tests would use
    is not worth it.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = add(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    )
    page_ids: list[int] = []
    content_ids: list[int] = []
    for text in pages:
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
        content_ids.append(
            add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))
        )
        page_ids.append(0)  # reserved; filled in below

    pages_id = len(objects) + len(pages) + 1
    for index, content_id in enumerate(content_ids):
        page_ids[index] = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font_id, content_id)
        )
    kids = b" ".join(b"%d 0 R" % page_id for page_id in page_ids)
    actual_pages_id = add(
        b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))
    )
    assert actual_pages_id == pages_id
    catalog_id = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += b"%010d 00000 n \n" % offset
    out += (
        b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
        % (len(objects) + 1, catalog_id, xref_at)
    )
    return bytes(out)


# ---------------------------------------------------------------- detection


def test_detect_kind_uses_signatures_not_extensions():
    # A PNG named .pdf is still a PNG. Content decides.
    assert attachments.detect_kind(png_bytes(), "invoice.pdf") == attachments.KIND_IMAGE
    assert attachments.detect_kind(jpeg_bytes(), "photo.txt") == attachments.KIND_IMAGE
    assert attachments.detect_kind(b"%PDF-1.7\n%...", "x.bin") == attachments.KIND_PDF
    assert attachments.detect_kind(b"print('hi')\n", "s.py") == attachments.KIND_TEXT


def test_detect_kind_separates_docx_from_xlsx_by_package_parts():
    assert attachments.detect_kind(docx_bytes(["hello"]), "a.xlsx") == attachments.KIND_DOCX
    assert (
        attachments.detect_kind(xlsx_bytes({"S": [[1]]}), "b.docx")
        == attachments.KIND_XLSX
    )


def test_unknown_zip_and_binary_are_refused():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "nothing useful")
    with pytest.raises(attachments.AttachmentError, match="ZIP-based"):
        attachments.detect_kind(buffer.getvalue(), "bundle.zip")

    with pytest.raises(attachments.AttachmentError, match="not a file type"):
        attachments.detect_kind(b"\x00\x01\x02\x03binary\x00", "thing.dat")


def test_empty_attachment_is_refused():
    with pytest.raises(attachments.AttachmentError):
        attachments.detect_kind(b"", "empty.txt")


# --------------------------------------------------------------- validation


def test_accept_records_image_identity():
    raw = png_bytes(64, 48)
    accepted = attachments.accept(raw, "  Screenshot 2026.png ")
    assert accepted.kind == attachments.KIND_IMAGE
    assert accepted.mime == "image/png"
    assert (accepted.width, accepted.height) == (64, 48)
    assert accepted.extension == ".png"
    assert accepted.byte_count == len(raw)


def test_accept_rejects_a_truncated_image():
    broken = png_bytes()[:40]
    with pytest.raises(attachments.AttachmentError):
        attachments.accept(broken, "broken.png")


def test_accept_enforces_the_size_ceiling():
    oversized = b"a" * (attachments.MAX_ATTACHMENT_BYTES + 1)
    with pytest.raises(attachments.AttachmentError, match="larger than"):
        attachments.accept(oversized, "huge.txt")


@pytest.mark.parametrize(
    "supplied,expected",
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\otisd\\secret.txt", "secret.txt"),
        ("/var/log/syslog", "syslog"),
        ("", "attachment"),
        ("   ", "attachment"),
    ],
)
def test_safe_filename_strips_every_path_component(supplied, expected):
    assert attachments.safe_filename(supplied) == expected


def test_safe_filename_bounds_length_and_keeps_the_suffix():
    name = attachments.safe_filename("x" * 500 + ".pdf")
    assert len(name) <= attachments.MAX_FILENAME_CHARS
    assert name.endswith(".pdf")


# --------------------------------------------------------------- extraction


def test_text_extraction_normalises_line_endings():
    accepted = attachments.accept(b"alpha\r\nbeta\rgamma\n", "notes.txt")
    extracted = attachments.extract(accepted)
    assert extracted.text == "alpha\nbeta\ngamma\n"
    assert extracted.method == "decoded"
    assert extracted.truncated is False


def test_text_extraction_truncates_and_says_so(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_EXTRACTED_CHARS", 100)
    accepted = attachments.accept(b"z" * 5_000, "long.log")
    extracted = attachments.extract(accepted)
    assert len(extracted.text) == 100
    assert extracted.truncated is True


def test_docx_extraction_reads_paragraphs_and_tables():
    raw = docx_bytes(
        ["Front camera calibration", "Target distance 1.2 m"],
        table=[["Item", "Spec"], ["Torque", "9 Nm"]],
    )
    extracted = attachments.extract(attachments.accept(raw, "procedure.docx"))
    assert "Front camera calibration" in extracted.text
    assert "Torque | 9 Nm" in extracted.text
    assert extracted.method == "python-docx"


def test_xlsx_extraction_labels_each_sheet():
    raw = xlsx_bytes({
        "ROs": [["RO", "Status"], [12345, "open"]],
        "Notes": [["needs map"]],
    })
    extracted = attachments.extract(attachments.accept(raw, "work.xlsx"))
    assert "--- sheet: ROs ---" in extracted.text
    assert "12345 | open" in extracted.text
    assert "--- sheet: Notes ---" in extracted.text


def test_pdf_extraction_labels_pages_and_counts_them():
    raw = pdf_bytes(["Front radar target 1.5 m", "Torque 9 Nm"])
    accepted = attachments.accept(raw, "procedure.pdf")
    assert accepted.kind == attachments.KIND_PDF
    extracted = attachments.extract(accepted)
    assert extracted.page_count == 2
    assert "--- page 1 ---" in extracted.text
    assert "--- page 2 ---" in extracted.text
    assert "Front radar target 1.5 m" in extracted.text
    assert "Torque 9 Nm" in extracted.text


def test_pdf_with_no_extractable_text_says_so_instead_of_guessing(monkeypatch):
    # A scan-only page: no embedded text, and OCR unavailable in this test.
    monkeypatch.setattr(attachments, "_ocr_pdf_pages", lambda raw, pages: (pages, 0))
    extracted = attachments.extract(attachments.accept(pdf_bytes([" "]), "scan.pdf"))
    assert extracted.text.strip() == ""
    assert "No text could be extracted" in (extracted.note or "")


def test_xlsx_extraction_stops_at_the_cell_ceiling(monkeypatch):
    monkeypatch.setattr(attachments, "MAX_XLSX_CELLS", 10)
    raw = xlsx_bytes({"Big": [[f"r{index}", index] for index in range(200)]})
    extracted = attachments.extract(attachments.accept(raw, "big.xlsx"))
    assert extracted.truncated is True
    assert "cells were read" in (extracted.note or "")


# ------------------------------------------------------------------ storage


def test_storage_is_content_addressed_and_round_trips(tmp_path):
    accepted = attachments.accept(b"torque spec 9 Nm", "spec.txt")
    extracted = attachments.extract(accepted)
    blob, sidecar = attachments.write_attachment(tmp_path, accepted, extracted)

    assert blob.name == f"{accepted.sha256}.txt"
    assert blob.read_bytes() == b"torque spec 9 Nm"
    assert sidecar.read_text(encoding="utf-8") == extracted.text
    assert attachments.read_extracted_text(
        tmp_path, accepted.sha256, accepted.extension
    ) == extracted.text


def test_a_text_attachment_keeps_its_original_bytes_beside_the_extraction(tmp_path):
    """The sidecar must never be written over the file it was extracted from.

    A .txt upload is the case that catches this: both paths are derived from
    the same digest, so a shared suffix would silently replace the original
    with its own normalised, possibly truncated, extraction.
    """
    raw = b"line one\r\nline two\r\n"
    accepted = attachments.accept(raw, "notes.txt")
    extracted = attachments.extract(accepted)
    blob, sidecar = attachments.write_attachment(tmp_path, accepted, extracted)

    assert blob != sidecar
    assert blob.read_bytes() == raw
    assert sidecar.read_text(encoding="utf-8") == "line one\nline two\n"


def test_storage_paths_refuse_a_forged_digest(tmp_path):
    with pytest.raises(attachments.AttachmentError, match="digest"):
        attachments.storage_paths(tmp_path, "../../escape", ".txt")


def test_storage_paths_neutralise_a_hostile_extension(tmp_path):
    blob, _ = attachments.storage_paths(tmp_path, "a" * 64, "../../evil")
    assert blob.parent == tmp_path
    assert blob.name.endswith(".bin")


def test_read_extracted_text_is_empty_when_the_sidecar_is_gone(tmp_path):
    assert attachments.read_extracted_text(tmp_path, "b" * 64, ".pdf") == ""


# -------------------------------------------------------- chat presentation


def test_excerpt_cuts_on_a_line_boundary():
    body = "\n".join(f"line {index}" for index in range(400))
    window, cut = attachments.excerpt(body, 200)
    assert cut is True
    assert not window.endswith("lin")
    assert len(window) <= 200


def test_excerpt_leaves_short_text_whole():
    window, cut = attachments.excerpt("short", 200)
    assert (window, cut) == ("short", False)


def test_message_block_names_the_file_and_flags_truncation():
    record = {
        "id": 7,
        "filename": "ro-12345.pdf",
        "kind": "pdf",
        "byte_count": 2048,
        "page_count": 12,
        "truncated": True,
        "note": "3 page(s) required OCR; that text may contain errors.",
    }
    block = attachments.message_block(record, "page one text")
    assert "ro-12345.pdf" in block
    assert "12 page(s)" in block
    assert "id 7" in block
    assert "required OCR" in block
    assert "read_attachment" in block


def test_message_block_is_honest_when_nothing_was_extracted():
    record = {"id": 3, "filename": "scan.pdf", "kind": "pdf", "byte_count": 10}
    assert "No text could be extracted" in attachments.message_block(record, "")


def test_compose_message_keeps_the_question_ahead_of_the_file():
    composed = attachments.compose_message("what is the torque spec?", ["[Attachment: a]"])
    assert composed.startswith("what is the torque spec?")
    assert "[Attachment: a]" in composed


def test_compose_message_handles_files_with_no_typed_text():
    composed = attachments.compose_message("", ["[Attachment: a]"])
    assert "no message text" in composed
    assert "[Attachment: a]" in composed


def test_compose_message_without_attachments_is_the_plain_text():
    assert attachments.compose_message("  hello  ", []) == "hello"


def test_artifact_exposes_metadata_but_never_content():
    record = {
        "id": 5,
        "filename": "photo.jpg",
        "kind": "image",
        "mime": "image/jpeg",
        "byte_count": 1024,
        "sha256": "c" * 64,
        "extraction_method": "vision",
        "extracted_chars": 400,
        "truncated": False,
        "note": None,
        "width": 800,
        "height": 600,
        "page_count": None,
    }
    card = attachments.artifact(record)
    assert card["type"] == "attachment"
    assert card["data"]["kind_label"] == "image"
    assert card["data"]["size_label"] == "1.0 KB"
    serialised = json.dumps(card)
    assert "vision" in serialised
    # The card is metadata only -- no bytes, no extracted body text.
    assert "data:image" not in serialised


# ------------------------------------------------------------ read_attachment


class FakeStore:
    def __init__(self, record):
        self.record = record

    def get_attachment(self, attachment_id, *, user_id=None):
        if attachment_id != self.record["id"]:
            return None
        return dict(self.record)


class FakeSettings:
    def __init__(self, directory):
        self.attachment_dir = directory


def build_reader(tmp_path, body: str, *, conversation_id=9):
    accepted = attachments.accept(body.encode("utf-8"), "manual.txt")
    extracted = attachments.extract(accepted)
    attachments.write_attachment(tmp_path, accepted, extracted)
    record = {
        "id": 4,
        "conversation_id": conversation_id,
        "filename": "manual.txt",
        "kind": "text",
        "sha256": accepted.sha256,
        "extension": accepted.extension,
    }
    return attachments.make_read_attachment(FakeSettings(tmp_path), FakeStore(record))


def test_read_attachment_pages_through_a_long_document(tmp_path):
    body = "".join(f"[{index:05d}]" for index in range(4_000))
    read = build_reader(tmp_path, body)

    first = read({"attachment_id": 4, "conversation_id": 9, "user_id": "otis"})
    assert first["ok"] is True
    assert first["offset"] == 0
    assert first["returned_chars"] == attachments.MAX_READ_CHARS
    assert first["has_more"] is True

    second = read({
        "attachment_id": 4,
        "conversation_id": 9,
        "user_id": "otis",
        "offset": first["next_offset"],
    })
    assert second["offset"] == first["next_offset"]
    assert second["text"] != first["text"]


def test_read_attachment_search_lands_on_the_match(tmp_path):
    body = ("filler\n" * 3_000) + "TORQUE SPEC 9 Nm\n" + ("tail\n" * 100)
    read = build_reader(tmp_path, body)
    result = read({
        "attachment_id": 4, "conversation_id": 9, "user_id": "otis", "search": "torque spec"
    })
    assert "TORQUE SPEC 9 Nm" in result["text"]
    assert result["match_at"] is not None


def test_read_attachment_reports_a_search_miss_without_inventing_text(tmp_path):
    read = build_reader(tmp_path, "nothing relevant here")
    result = read({
        "attachment_id": 4, "conversation_id": 9, "user_id": "otis", "search": "camshaft"
    })
    assert result["ok"] is True
    assert result["text"] == ""
    assert "does not appear" in result["note"]


def test_read_attachment_refuses_a_file_from_another_conversation(tmp_path):
    read = build_reader(tmp_path, "secret contents", conversation_id=9)
    result = read({"attachment_id": 4, "conversation_id": 77, "user_id": "otis"})
    assert result["ok"] is False
    assert "not part of this conversation" in result["error"]
    assert "secret" not in json.dumps(result)


def test_read_attachment_refuses_an_unknown_id(tmp_path):
    read = build_reader(tmp_path, "body")
    assert read({"attachment_id": 999, "conversation_id": 9})["ok"] is False


def test_read_attachment_rejects_a_non_integer_id(tmp_path):
    read = build_reader(tmp_path, "body")
    result = read({"attachment_id": "4", "conversation_id": 9})
    assert result["ok"] is False
    assert "integer" in result["error"]
