"""X Omni service package wiring.

ADAS SI OCR is installed at package import so every AdasSI instance—including
search, Calibration IQ research, and direct document opening—uses the same
page-level OCR path without a parallel OCR-only service.

The post-collision research operator is also installed here. It adds the
licensed ALLDATA/public-OEM research capability and same-origin mobile browser
handoff without putting provider credentials into model context. Public OEM
findings can be preserved into ADAS SI, and a separate read-only setup route
makes explicit ALLDATA credential requests deterministic. Final policy guarding
keeps installed handlers subordinate to config/tools.yaml.
"""

from . import adas_si as _adas_si
from . import adas_ocr as _adas_ocr
from . import research_operator as _research_operator
from . import research_capture as _research_capture
from . import research_setup as _research_setup
from . import research_vault as _research_vault
from . import research_policy as _research_policy

_adas_ocr.install_class(_adas_si.AdasSI)
_research_operator.install()
_research_vault.install()
_research_capture.install()
_research_setup.install()
_research_policy.install()
