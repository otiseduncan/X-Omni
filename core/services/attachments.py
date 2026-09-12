"""X Omni -- operator file attachments.

A file the operator attaches in chat becomes *durable text* in the
conversation, not a transient upload. That is deliberate and matches how
camera stills already work here: bytes are validated once, converted to text
the conversation model can actually read, and the text is what survives
context packing, reconnects, and history replay.

Five kinds are accepted:

    image   JPEG/PNG/WebP -- transcribed and described by the vision worker
    pdf     embedded text via pypdf/PDFium, RapidOCR for scan-only pages
    text    plain text, source code, CSV, JSON, Markdown, logs
    docx    Word paragraphs and table cells
    xlsx    Excel cell values, per sheet

Extraction is bounded twice. The full result is written beside the original
bytes so ``read_attachment`` can page through a long document on demand; only
a short head excerpt is folded into the chat message, so attaching a 300-page
PDF cannot blow the model's context window.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import re
import unicodedata
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger("xomni.attachments")

KIND_IMAGE = "image"
KIND_PDF = "pdf"
KIND_TEXT = "text"
KIND_DOCX = "docx"
KIND_XLSX = "xlsx"

ATTACHMENT_KINDS = frozenset({KIND_IMAGE, KIND_PDF, KIND_TEXT, KIND_DOCX, KIND_XLSX})

# One attachment, before extraction. Generous enough for a phone photo or a
# service manual chapter; small enough that a stray upload cannot fill the disk.
MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_EDGE = 12_288
# The vision worker sees a bounded copy, never the original megapixels.
VISION_MAX_EDGE = 1_568
VISION_COMPLETION_TOKENS = 1_200
VISION_TIMEOUT_SECONDS = 180.0

# Persisted extraction ceiling. Beyond this the stored text is truncated and
# the record says so -- it never silently pretends to be the whole document.
MAX_EXTRACTED_CHARS = 600_000
MAX_PDF_PAGES = 400
MAX_XLSX_CELLS = 50_000
# What actually enters the chat message, and therefore the model's context.
CONTEXT_EXCERPT_CHARS = 6_000
# One read_attachment call's ceiling.
MAX_READ_CHARS = 12_000

MAX_FILENAME_CHARS = 180
MAX_ATTACHMENTS_PER_MESSAGE = 8

IMAGE_TRANSCRIPTION_PROMPT = (
    "Transcribe and describe this image for someone who cannot see it. "
    "First, transcribe every piece of visible text verbatim -- labels, part "
    "numbers, torque values, error codes, table rows, handwriting -- keeping "
    "the original grouping where you can. Then describe what the image shows. "
    "Report only what the pixels support; if something is cut off, blurred, "
    "or unreadable, say so plainly instead of guessing."
)

_IMAGE_FORMAT_TO_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}

_KIND_EXTENSIONS = {
    KIND_PDF: ".pdf",
    KIND_DOCX: ".docx",
    KIND_XLSX: ".xlsx",
}

_MIME_BY_KIND = {
    KIND_PDF: "application/pdf",
    KIND_DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    KIND_XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_IMAGE_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}

# Extensions accepted as plain text. Anything else without a recognized binary
# signature must still decode as text before it is accepted.
TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".env", ".xml", ".html", ".htm", ".css", ".svg",
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".java", ".kt", ".go", ".rs",
    ".rb", ".php", ".swift", ".m", ".scala", ".sh", ".bash", ".zsh",
    ".ps1", ".psm1", ".bat", ".cmd", ".sql", ".r", ".lua", ".pl", ".vb",
    ".gradle", ".properties", ".diff", ".patch", ".srt", ".vtt",
})

KIND_LABELS = {
    KIND_IMAGE: "image",
    KIND_PDF: "PDF",
    KIND_TEXT: "text file",
    KIND_DOCX: "Word document",
    KIND_XLSX: "Excel workbook",
}


class AttachmentError(ValueError):
    """One attachment could not be accepted or read."""


@dataclass(frozen=True)
class ExtractedText:
    """Text recovered from one attachment, plus how it was recovered."""

    text: str
    method: str
    page_count: Optional[int] = None
    truncated: bool = False
    note: Optional[str] = None


@dataclass(frozen=True)
class AcceptedFile:
    """One validated attachment, before extraction."""

    raw: bytes
    kind: str
    mime: str
    filename: str
    extension: str
    sha256: str
    width: Optional[int] = None
    height: Optional[int] = None

    @property
    def byte_count(self) -> int:
        return len(self.raw)


# ---------------------------------------------------------------- filenames


def safe_filename(value: object) -> str:
    """Reduce a client-supplied name to a bounded, path-free display label.

    This never builds a filesystem path -- stored files are named by content
    hash -- but it is shown in chat and echoed to the model, so it must not
    carry separators, control characters, or unbounded length.
    """
    name = str(value or "").strip()
    # Some platforms send a full path in the multipart filename; keep the leaf.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(
        character for character in name if unicodedata.category(character)[0] != "C"
    )
    name = re.sub(r'[<>:"|?*]', "", name).strip(" .")
    if not name:
        name = "attachment"
    if len(name) > MAX_FILENAME_CHARS:
        stem, dot, suffix = name.rpartition(".")
        if dot and 0 < len(suffix) <= 12:
            name = f"{stem[: MAX_FILENAME_CHARS - len(suffix) - 1]}.{suffix}"
        else:
            name = name[:MAX_FILENAME_CHARS]
    return name


def _extension(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix if 0 < len(suffix) <= 12 else ""


# ---------------------------------------------------------------- detection


def _image_mime(raw: bytes) -> Optional[str]:
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _ooxml_kind(raw: bytes) -> Optional[str]:
    """Tell .docx from .xlsx by the parts actually inside the ZIP.

    Both are ZIP containers with the same signature, so an extension is not
    evidence of anything. The package parts are.
    """
    if not raw.startswith(b"PK\x03\x04"):
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return None
    if "word/document.xml" in names:
        return KIND_DOCX
    if "xl/workbook.xml" in names:
        return KIND_XLSX
    return None


def _looks_like_text(raw: bytes) -> bool:
    """Whether the leading bytes decode as text and carry no embedded NULs."""
    sample = raw[:65_536]
    if b"\x00" in sample:
        return False
    for encoding in ("utf-8", "utf-16", "cp1252"):
        try:
            sample.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        return True
    return False


def detect_kind(raw: bytes, filename: str) -> str:
    """Classify one attachment from its bytes, using the name only as a hint.

    Content signatures decide. Plain text has no signature, so it is the one
    kind an extension can suggest -- and even then the bytes must decode.
    """
    if not raw:
        raise AttachmentError("The attachment is empty.")

    if _image_mime(raw) is not None:
        return KIND_IMAGE
    if raw.startswith(b"%PDF-"):
        return KIND_PDF
    ooxml = _ooxml_kind(raw)
    if ooxml is not None:
        return ooxml
    if raw.startswith(b"PK\x03\x04"):
        raise AttachmentError(
            f"'{filename}' is a ZIP-based file X does not read. Attach a PDF, "
            f"Word (.docx), Excel (.xlsx), image, or text file."
        )
    if _looks_like_text(raw):
        return KIND_TEXT
    raise AttachmentError(
        f"'{filename}' is not a file type X can read. Attach a PDF, Word "
        f"(.docx), Excel (.xlsx), image (JPEG/PNG/WebP), or text file."
    )


# --------------------------------------------------------------- validation


def _validate_image(raw: bytes, filename: str) -> tuple[str, int, int]:
    if len(raw) > MAX_IMAGE_BYTES:
        raise AttachmentError(
            f"'{filename}' is larger than the "
            f"{MAX_IMAGE_BYTES // (1024 * 1024)} MiB image limit."
        )
    magic = _image_mime(raw)
    if magic is None:
        raise AttachmentError(f"'{filename}' is not a JPEG, PNG, or WebP image.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                detected = _IMAGE_FORMAT_TO_MIME.get(str(image.format or "").upper())
                width, height = image.size
                if detected != magic:
                    raise AttachmentError(
                        f"'{filename}' does not decode as the format its "
                        f"signature claims."
                    )
                if width <= 0 or height <= 0:
                    raise AttachmentError(f"'{filename}' has invalid dimensions.")
                if width > MAX_IMAGE_EDGE or height > MAX_IMAGE_EDGE:
                    raise AttachmentError(
                        f"'{filename}' exceeds the {MAX_IMAGE_EDGE:,}-pixel edge limit."
                    )
                if width * height > MAX_IMAGE_PIXELS:
                    raise AttachmentError(
                        f"'{filename}' exceeds the {MAX_IMAGE_PIXELS:,}-pixel limit."
                    )
                # Force real pixel decoding: a truncated or spoofed header must
                # fail here, not inside the model worker.
                image.load()
    except AttachmentError:
        raise
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise AttachmentError(f"'{filename}' could not be decoded safely.") from exc
    return magic, int(width), int(height)


def accept(raw: bytes, filename: object) -> AcceptedFile:
    """Validate one uploaded attachment and return its content identity."""
    name = safe_filename(filename)
    if not raw:
        raise AttachmentError(f"'{name}' is empty.")
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(
            f"'{name}' is larger than the "
            f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB attachment limit."
        )

    kind = detect_kind(raw, name)
    width: Optional[int] = None
    height: Optional[int] = None

    if kind == KIND_IMAGE:
        mime, width, height = _validate_image(raw, name)
        extension = _IMAGE_EXTENSIONS[mime]
    elif kind == KIND_TEXT:
        if len(raw) > MAX_TEXT_BYTES:
            raise AttachmentError(
                f"'{name}' is larger than the "
                f"{MAX_TEXT_BYTES // (1024 * 1024)} MiB text-file limit."
            )
        mime = "text/plain"
        extension = _extension(name) or ".txt"
    else:
        mime = _MIME_BY_KIND[kind]
        extension = _KIND_EXTENSIONS[kind]

    return AcceptedFile(
        raw=raw,
        kind=kind,
        mime=mime,
        filename=name,
        extension=extension,
        sha256=hashlib.sha256(raw).hexdigest(),
        width=width,
        height=height,
    )


# --------------------------------------------------------------- extraction


def _clamp(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_EXTRACTED_CHARS:
        return text, False
    return text[:MAX_EXTRACTED_CHARS], True


def _decode_text(raw: bytes, filename: str) -> str:
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Every candidate failed on some byte. Decode lossily rather than refuse a
    # file the operator can plainly read -- and say so in the record.
    log.info("attachment %s decoded with replacement characters", filename)
    return raw.decode("utf-8", errors="replace")


def _extract_text_file(file: AcceptedFile) -> ExtractedText:
    decoded = _decode_text(file.raw, file.filename)
    text, truncated = _clamp(decoded.replace("\r\n", "\n").replace("\r", "\n"))
    return ExtractedText(text=text, method="decoded", truncated=truncated)


def _read_pdf_pages(raw: bytes, filename: str) -> tuple[list[str], str]:
    """Embedded PDF text, pypdf first and PDFium as the supported fallback."""
    pages: Optional[list[str]] = None
    method = ""
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None  # noqa: N806 - optional dependency
    try:
        import pypdfium2 as pdfium
    except ImportError:
        pdfium = None

    if PdfReader is not None:
        try:
            reader = PdfReader(io.BytesIO(raw), strict=False)
            pages = [
                str(page.extract_text() or "")
                for page in reader.pages[:MAX_PDF_PAGES]
            ]
            method = "pypdf"
        except Exception:  # noqa: BLE001 - PDFium is the supported fallback
            if pdfium is None:
                raise AttachmentError(
                    f"'{filename}' could not be read as a PDF."
                ) from None

    if (pages is None or not any(page.strip() for page in pages)) and pdfium is not None:
        native = pages
        document = None
        try:
            document = pdfium.PdfDocument(io.BytesIO(raw))
            extracted: list[str] = []
            for index in range(min(len(document), MAX_PDF_PAGES)):
                page = document[index]
                try:
                    text_page = page.get_textpage()
                    try:
                        extracted.append(text_page.get_text_range() or "")
                    finally:
                        text_page.close()
                finally:
                    page.close()
            pages = extracted
            method = "pdfium"
        except Exception:  # noqa: BLE001 - keep an honest pypdf result
            if native is None:
                raise AttachmentError(
                    f"'{filename}' could not be read as a PDF."
                ) from None
            pages = native
        finally:
            if document is not None:
                document.close()

    if pages is None:
        raise AttachmentError(
            "Neither pypdf nor pypdfium2 is installed; X cannot read PDFs."
        )
    return pages, method or "pypdf"


def _ocr_pdf_pages(raw: bytes, pages: list[str]) -> tuple[list[str], int]:
    """OCR only the pages whose embedded text is unusable.

    A scanned repair order has no embedded text at all; a mixed document has a
    few such pages. Rendering and OCR are expensive, so pages that already
    carry real text are left alone.
    """
    from . import adas_ocr

    try:
        import pypdfium2 as pdfium
    except ImportError:
        return pages, 0

    weak = [
        index
        for index, text in enumerate(pages)
        if not adas_ocr.usable_native_text(text)
    ]
    if not weak:
        return pages, 0

    output = list(pages)
    recovered = 0
    document = None
    try:
        document = pdfium.PdfDocument(io.BytesIO(raw))
        for index in weak:
            if index >= len(document):
                continue
            try:
                page = document[index]
                try:
                    scale = adas_ocr.OCR_RENDER_WIDTH / max(1.0, page.get_width())
                    bitmap = page.render(scale=max(1.0, min(scale, 4.0)))
                    image = bitmap.to_pil()
                    try:
                        buffer = io.BytesIO()
                        image.save(buffer, format="PNG")
                    finally:
                        image.close()
                finally:
                    page.close()
                result = adas_ocr.ocr_png_bytes(buffer.getvalue())
            except Exception as exc:  # noqa: BLE001 - keep the honest native text
                log.warning(
                    "attachment OCR failed on page %s: %s", index + 1, type(exc).__name__
                )
                continue
            text = str(result.get("text") or "")
            if adas_ocr.usable_ocr_text(text, result.get("confidence")):
                output[index] = text
                recovered += 1
    except Exception as exc:  # noqa: BLE001 - OCR is best-effort enrichment
        log.warning("attachment OCR pass failed: %s", type(exc).__name__)
    finally:
        if document is not None:
            document.close()
    return output, recovered


def _extract_pdf(file: AcceptedFile) -> ExtractedText:
    pages, method = _read_pdf_pages(file.raw, file.filename)
    pages, recovered = _ocr_pdf_pages(file.raw, pages)
    if recovered:
        method = f"{method}+ocr"

    body = "\n\n".join(
        f"--- page {number} ---\n{text.strip()}"
        for number, text in enumerate(pages, 1)
        if text.strip()
    )
    note = None
    if not body.strip():
        note = (
            "No text could be extracted from this PDF. It may be an image-only "
            "scan that local OCR could not read."
        )
    elif recovered:
        note = f"{recovered} page(s) required OCR; that text may contain errors."

    text, truncated = _clamp(body)
    return ExtractedText(
        text=text,
        method=method,
        page_count=len(pages),
        truncated=truncated,
        note=note,
    )


def _extract_docx(file: AcceptedFile) -> ExtractedText:
    try:
        import docx  # python-docx
    except ImportError as exc:
        raise AttachmentError(
            "python-docx is not installed; X cannot read Word documents. "
            "Run X Omni setup to install it."
        ) from exc

    try:
        document = docx.Document(io.BytesIO(file.raw))
    except Exception as exc:  # noqa: BLE001 - a corrupt package is the operator's answer
        raise AttachmentError(
            f"'{file.filename}' could not be read as a Word document."
        ) from exc

    blocks = [
        paragraph.text.strip()
        for paragraph in document.paragraphs
        if paragraph.text and paragraph.text.strip()
    ]
    for index, table in enumerate(document.tables, 1):
        rows = [
            " | ".join(cell.text.strip().replace("\n", " ") for cell in row.cells)
            for row in table.rows
        ]
        rows = [row for row in rows if row.replace("|", "").strip()]
        if rows:
            blocks.append(f"--- table {index} ---\n" + "\n".join(rows))

    text, truncated = _clamp("\n\n".join(blocks))
    return ExtractedText(
        text=text,
        method="python-docx",
        truncated=truncated,
        note=None if text.strip() else "This Word document contains no readable text.",
    )


def _extract_xlsx(file: AcceptedFile) -> ExtractedText:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise AttachmentError(
            "openpyxl is not installed; X cannot read Excel workbooks. "
            "Run X Omni setup to install it."
        ) from exc

    try:
        workbook = load_workbook(
            io.BytesIO(file.raw), read_only=True, data_only=True
        )
    except Exception as exc:  # noqa: BLE001
        raise AttachmentError(
            f"'{file.filename}' could not be read as an Excel workbook."
        ) from exc

    blocks: list[str] = []
    cells_read = 0
    truncated = False
    try:
        for sheet in workbook.worksheets:
            rows: list[str] = []
            for row in sheet.iter_rows(values_only=True):
                if cells_read >= MAX_XLSX_CELLS:
                    truncated = True
                    break
                cells_read += len(row)
                values = [
                    "" if value is None else str(value).replace("\n", " ").strip()
                    for value in row
                ]
                while values and not values[-1]:
                    values.pop()
                if values:
                    rows.append(" | ".join(values))
            if rows:
                blocks.append(f"--- sheet: {sheet.title} ---\n" + "\n".join(rows))
            if truncated:
                break
    finally:
        workbook.close()

    text, clamped = _clamp("\n\n".join(blocks))
    return ExtractedText(
        text=text,
        method="openpyxl",
        truncated=truncated or clamped,
        note=(
            f"Only the first {MAX_XLSX_CELLS:,} cells were read."
            if truncated
            else (None if text.strip() else "This workbook contains no readable cells.")
        ),
    )


_EXTRACTORS = {
    KIND_TEXT: _extract_text_file,
    KIND_PDF: _extract_pdf,
    KIND_DOCX: _extract_docx,
    KIND_XLSX: _extract_xlsx,
}


def extract(file: AcceptedFile) -> ExtractedText:
    """Recover text from one non-image attachment. CPU-bound; call in a thread."""
    extractor = _EXTRACTORS.get(file.kind)
    if extractor is None:
        raise AttachmentError(f"'{file.filename}' has no text extractor.")
    return extractor(file)


async def extract_async(file: AcceptedFile) -> ExtractedText:
    """Extract off the event loop so a long PDF cannot stall Core."""
    return await asyncio.to_thread(extract, file)


# ------------------------------------------------------------------- vision


def vision_copy(file: AcceptedFile) -> tuple[bytes, str]:
    """Bounded JPEG/PNG copy of an image for the vision worker.

    A 12-megapixel phone photo is downscaled before it is base64-encoded into
    a model request; sending the original would waste context and time for no
    gain in what the worker can actually resolve.
    """
    with Image.open(io.BytesIO(file.raw)) as opened:
        image = ImageOps.exif_transpose(opened)
        if max(image.size) <= VISION_MAX_EDGE and file.mime != "image/webp":
            return file.raw, file.mime
        image.thumbnail((VISION_MAX_EDGE, VISION_MAX_EDGE), Image.LANCZOS)
        buffer = io.BytesIO()
        if image.mode in {"RGBA", "LA", "P"}:
            image.convert("RGB").save(buffer, format="JPEG", quality=88)
        else:
            image.convert("RGB").save(buffer, format="JPEG", quality=88)
    return buffer.getvalue(), "image/jpeg"


def vision_messages(file: AcceptedFile, prompt: str = "") -> list[dict]:
    """Build the single-image request sent to the active vision worker."""
    import base64

    raw, mime = vision_copy(file)
    encoded = base64.b64encode(raw).decode("ascii")
    instruction = str(prompt or "").strip() or IMAGE_TRANSCRIPTION_PROMPT
    return [
        {
            "role": "system",
            "content": (
                "You are reading one image file the operator attached to a chat "
                "message. Describe and transcribe only what the pixels support. "
                "Do not claim you opened a camera, browsed anything, or saw more "
                "than this single image."
            ),
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": instruction},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                },
            ],
        },
    ]


async def describe_image(router, file: AcceptedFile, prompt: str = "") -> ExtractedText:
    """Transcribe and describe one image with the active vision worker.

    The caller ensures a vision-capable worker is active; swapping policy
    differs between a live upload and a background tick, so it is not decided
    here.
    """
    from ..models.client import ModelClient  # local import avoids a cycle

    client = ModelClient(router, temperature=0.0)
    description = await asyncio.wait_for(
        client.complete(
            vision_messages(file, prompt),
            max_tokens=VISION_COMPLETION_TOKENS,
            temperature=0.0,
        ),
        timeout=VISION_TIMEOUT_SECONDS,
    )
    body = str(description or "").strip()
    if not body:
        raise AttachmentError("The vision worker returned an empty description.")
    text, truncated = _clamp(body)
    return ExtractedText(
        text=text,
        method="vision",
        truncated=truncated,
        note=(
            "This is X's reading of the image, not the image itself. Details "
            "not mentioned here were not recorded."
        ),
    )


# ------------------------------------------------------------------ storage


#: Distinguishes the extracted-text sidecar from the original. It must not
#: collide with any real attachment extension: a .txt upload would otherwise
#: resolve both paths to the same file and the sidecar would overwrite the
#: very bytes it was extracted from.
EXTRACTED_TEXT_SUFFIX = ".extracted.txt"


def storage_paths(directory: Path, sha256: str, extension: str) -> tuple[Path, Path]:
    """Content-addressed original and its extracted-text sidecar.

    Naming by digest means re-attaching the same file costs nothing and a
    client-supplied name can never steer a write outside the directory.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", str(sha256 or "")):
        raise AttachmentError("Attachment digest is invalid.")
    suffix = str(extension or "")
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,11}", suffix):
        suffix = ".bin"
    return (
        directory / f"{sha256}{suffix}",
        directory / f"{sha256}{EXTRACTED_TEXT_SUFFIX}",
    )


def write_attachment(
    directory: Path, file: AcceptedFile, extracted: ExtractedText
) -> tuple[Path, Path]:
    """Persist the original bytes and the full extracted text."""
    directory.mkdir(parents=True, exist_ok=True)
    blob_path, text_path = storage_paths(directory, file.sha256, file.extension)
    if not blob_path.exists():
        blob_path.write_bytes(file.raw)
    text_path.write_text(extracted.text, encoding="utf-8")
    return blob_path, text_path


def read_extracted_text(directory: Path, sha256: str, extension: str) -> str:
    _, text_path = storage_paths(directory, sha256, extension)
    if not text_path.is_file():
        return ""
    return text_path.read_text(encoding="utf-8", errors="replace")


# -------------------------------------------------------- chat presentation


def excerpt(text: str, limit: int = CONTEXT_EXCERPT_CHARS) -> tuple[str, bool]:
    """Head excerpt of extracted text, cut on a line boundary when possible."""
    body = str(text or "")
    if len(body) <= limit:
        return body, False
    window = body[:limit]
    cut = window.rfind("\n")
    if cut > limit // 2:
        window = window[:cut]
    return window.rstrip(), True


def human_size(byte_count: int) -> str:
    size = float(max(0, int(byte_count)))
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MB"


def artifact(record: dict) -> dict:
    """Chat card for one stored attachment. Metadata only -- never the bytes."""
    return {
        "type": "attachment",
        "data": {
            "ok": True,
            "id": record.get("id"),
            "filename": record.get("filename"),
            "kind": record.get("kind"),
            "kind_label": KIND_LABELS.get(str(record.get("kind")), "file"),
            "mime": record.get("mime"),
            "bytes": record.get("byte_count"),
            "size_label": human_size(int(record.get("byte_count") or 0)),
            "sha256": record.get("sha256"),
            "page_count": record.get("page_count"),
            "width": record.get("width"),
            "height": record.get("height"),
            "extraction_method": record.get("extraction_method"),
            "extracted_chars": record.get("extracted_chars"),
            "truncated": bool(record.get("truncated")),
            "note": record.get("note"),
        },
    }


def message_block(record: dict, text: str) -> str:
    """Render one attachment as the text block folded into a chat message.

    This is what the conversation model actually reads, so it states plainly
    what the content is, where it came from, and whether it is complete.
    """
    kind = str(record.get("kind") or "")
    label = KIND_LABELS.get(kind, "file")
    header = f"[Attachment: {record.get('filename')} -- {label}"
    if record.get("page_count"):
        header += f", {int(record['page_count'])} page(s)"
    if record.get("byte_count"):
        header += f", {human_size(int(record['byte_count']))}"
    header += f", id {record.get('id')}]"

    body, cut = excerpt(text)
    lines = [header]
    if kind == KIND_IMAGE:
        lines.append("X's reading of this image:")
    if body.strip():
        lines.append(body)
    else:
        lines.append("(No text could be extracted from this file.)")
    note = str(record.get("note") or "").strip()
    if note:
        lines.append(f"Note: {note}")
    if cut or record.get("truncated"):
        lines.append(
            f"[Truncated. This is the beginning of the file; call "
            f"read_attachment with attachment_id {record.get('id')} and an "
            f"offset to read further.]"
        )
    return "\n".join(lines)


def make_read_attachment(settings, store):
    """Model tool: read further into a file attached in this conversation.

    Registry injects the authoritative conversation and user; the model
    supplies only an attachment id, so it cannot reach a file from another
    conversation by asking for one.
    """

    def read_attachment(args: dict) -> dict:
        conversation_id = args.get("conversation_id")
        raw_id = args.get("attachment_id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            return {
                "ok": False,
                "error": "attachment_id must be the integer id shown in the message.",
            }

        record = store.get_attachment(raw_id, user_id=args.get("user_id") or None)
        if record is None or record.get("conversation_id") != conversation_id:
            return {
                "ok": False,
                "error": (
                    f"Attachment {raw_id} is not part of this conversation. Use an "
                    f"id shown in one of this conversation's messages."
                ),
            }

        text = read_extracted_text(
            Path(settings.attachment_dir),
            str(record["sha256"]),
            str(record["extension"]),
        )
        if not text:
            return {
                "ok": True,
                "attachment_id": raw_id,
                "filename": record["filename"],
                "total_chars": 0,
                "text": "",
                "has_more": False,
                "note": "No text was extracted from this file.",
            }

        offset = args.get("offset")
        start = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0
        start = max(0, min(start, len(text)))

        search = str(args.get("search") or "").strip()
        matched = None
        if search:
            found = text.lower().find(search.lower(), start)
            if found == -1:
                return {
                    "ok": True,
                    "attachment_id": raw_id,
                    "filename": record["filename"],
                    "total_chars": len(text),
                    "text": "",
                    "has_more": False,
                    "note": (
                        f"'{search}' does not appear in {record['filename']} at or "
                        f"after character {start}."
                    ),
                }
            # Start a little before the match so the reader sees its context.
            matched = found
            start = max(0, found - 200)

        window = text[start:start + MAX_READ_CHARS]
        return {
            "ok": True,
            "attachment_id": raw_id,
            "filename": record["filename"],
            "kind": record["kind"],
            "offset": start,
            "match_at": matched,
            "returned_chars": len(window),
            "total_chars": len(text),
            "has_more": start + len(window) < len(text),
            "next_offset": start + len(window),
            "text": window,
        }

    return read_attachment


def compose_message(user_text: str, blocks: list[str]) -> str:
    """Combine the operator's typed message with its attachment blocks.

    The typed message leads: an attachment is context for what was asked, and
    burying the question under a 6,000-character document changes what the
    model treats as the request.
    """
    typed = str(user_text or "").strip()
    if not blocks:
        return typed
    attached = "\n\n".join(blocks)
    if not typed:
        return (
            "(The operator attached the following with no message text.)\n\n"
            f"{attached}"
        )
    return f"{typed}\n\n{attached}"
