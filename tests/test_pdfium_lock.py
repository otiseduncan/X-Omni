"""PDFium must be used under Core's one shared lock.

On 2026-09-17 Core died twice in pdfium.dll (exception 0x80000003, no Python
traceback, nothing in the log) while two scanned ADAS Map PDFs were being read.
Unlocked concurrent PDFium use aborts the process, so the concurrency check runs
in a child process and inspects its exit code.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pdfium_call_sites() -> list[tuple[Path, int, bool]]:
    """Every ``pdfium.PdfDocument(...)`` call in Core and whether a PDFium lock guards it."""

    sites: list[tuple[Path, int, bool]] = []
    for path in sorted((ROOT / "core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "PdfDocument"
            ):
                continue
            guarded = False
            current: ast.AST | None = node
            while current is not None:
                if isinstance(current, ast.With) and any(
                    "PDFIUM_LOCK" in ast.unparse(item.context_expr) for item in current.items
                ):
                    guarded = True
                    break
                current = parents.get(current)
            sites.append((path.relative_to(ROOT), node.lineno, guarded))
    return sites


def test_every_pdfium_document_is_opened_under_the_shared_lock() -> None:
    sites = _pdfium_call_sites()
    assert sites, "expected PDFium call sites in Core"
    unguarded = [f"{path}:{line}" for path, line, guarded in sites if not guarded]
    assert not unguarded, f"PDFium opened without PDFIUM_LOCK: {unguarded}"


def test_module_locks_are_the_one_shared_lock() -> None:
    from core.services import adas_si
    from core.services.pdfium_lock import PDFIUM_LOCK

    assert adas_si._PDFIUM_LOCK is PDFIUM_LOCK


def test_concurrent_core_pdf_reads_do_not_abort_the_process(tmp_path) -> None:
    pytest.importorskip("pypdfium2")
    pytest.importorskip("PIL")
    script = textwrap.dedent(
        f"""
        import io, sys, threading
        sys.path.insert(0, {str(ROOT)!r})
        from PIL import Image
        import pypdfium2 as pdfium
        from core.services import attachments
        from core.services.pdfium_lock import PDFIUM_LOCK

        # An image-only PDF, like a scanned ADAS Map: no embedded text, so the
        # attachment reader falls through to PDFium.
        buffer = io.BytesIO()
        pages = [Image.new("RGB", (1275, 1650), (255, 255, 255)) for _ in range(4)]
        pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
        raw = buffer.getvalue()

        errors = []

        def read():
            try:
                for _ in range(25):
                    attachments._read_pdf_pages(raw, "scan.pdf")
            except Exception as exc:
                errors.append(repr(exc))

        def render():
            try:
                for _ in range(25):
                    with PDFIUM_LOCK:
                        document = pdfium.PdfDocument(io.BytesIO(raw))
                        try:
                            for index in range(len(document)):
                                page = document[index]
                                page.render(scale=2).to_pil()
                                page.close()
                        finally:
                            document.close()
            except Exception as exc:
                errors.append(repr(exc))

        threads = [threading.Thread(target=read), threading.Thread(target=read),
                   threading.Thread(target=render), threading.Thread(target=render)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        print("survived", errors)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=240,
        cwd=tmp_path,
    )
    assert completed.returncode == 0, (completed.returncode, completed.stderr[-2000:])
    assert "survived []" in completed.stdout, completed.stdout[-2000:]
