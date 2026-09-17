"""One process-wide lock for every PDFium call in Core.

PDFium's C library is not safe for concurrent use across threads, and a
violation aborts the whole process instead of raising: on 2026-09-17 Core died
twice in pdfium.dll (exception 0x80000003) with nothing in its log, while chat
attachments, the ADAS SI reader, and the artifact catalog each opened PDFs on
their own threads. A lock per module does not help; every caller must share
this one, and hold it from opening a document until it is closed.
"""

from __future__ import annotations

import threading

# Reentrant so a guarded helper may call another guarded helper.
PDFIUM_LOCK = threading.RLock()
