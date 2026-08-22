"""X Omni service package wiring.

ADAS SI OCR is installed at package import so every AdasSI instance—including
search, Calibration IQ research, and direct document opening—uses the same
page-level OCR path without a parallel OCR-only service.
"""

from . import adas_si as _adas_si
from . import adas_ocr as _adas_ocr

_adas_ocr.install_class(_adas_si.AdasSI)
