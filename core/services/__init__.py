"""X Omni service package wiring.

ADAS SI OCR is installed at package import so every AdasSI instance—including
search, Calibration IQ research, and direct document opening—uses the same
page-level OCR path without a parallel OCR-only service.

The post-collision research operator is also installed here. It adds the
licensed ALLDATA/public-OEM research capability and same-origin mobile browser
handoff without putting provider credentials into model context. A separate
read-only setup route makes explicit ALLDATA credential requests deterministic.
"""

from . import adas_si as _adas_si
from . import adas_ocr as _adas_ocr
from . import research_operator as _research_operator
from . import research_setup as _research_setup

_adas_ocr.install_class(_adas_si.AdasSI)
_research_operator.install()
_research_setup.install()
