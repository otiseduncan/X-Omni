"""X Omni service package wiring.

ADAS SI OCR is installed at package import so every AdasSI instance—including
search, Calibration IQ research, and direct document opening—uses the same
page-level OCR path without a parallel OCR-only service. Explicit vehicle
queries are identity-filtered by year/make/model so cross-vehicle topic matches
cannot become repair evidence, and calibration questions deep-scan every page of
the relevant OEM PDFs for buried requirement/trigger language.

The post-collision research operator adds licensed ALLDATA/public-OEM research,
mobile browser handoff, preservation into ADAS SI, and a verified composite
workflow that records which sources were actually searched. Ordinary calibration
questions automatically enter that deep lane unless explicitly local-only.
ALLDATA research follows the documented vehicle-first Repair/Collision workflow;
when a live model client is available it drives that workflow turn-by-turn
through the same collision_research tool actions available in ordinary chat
(research_alldata_agent), falling back to a fixed navigation sequence otherwise.
Either way, "verified" is decided once, centrally, by research_verification --
never by whether the browser merely stayed on an alldata.com URL. A persistent
ResearchTask (research_task/research_task_continuity) carries vehicle/subject
continuity across follow-ups generically, from message shape and parsed vehicle
identity, rather than an enumerated table of follow-up phrasings.
When an exact calibration/reset procedure is missing locally and ALLDATA produces
a verified exact-vehicle procedure, that source is acquired into ADAS SI with
URL-based deduplication and a second, independent vehicle-identity check at the
moment of capture. The cooperative Quick Reference collector provides a separate,
low-rate path for the weekly Calibration IQ queue: the operator manually selects
the exact CIQ vehicle in ALLDATA, then X enumerates that vehicle's ADAS Quick
Reference procedure links sequentially, skips duplicates by canonical URL/hash
(and conservative same-vehicle/title identity), preserves real revisions, OCRs
and indexes new PDFs, and tells X to run the existing CIQ research_ro contract to
link the refreshed ADAS SI evidence back to the repair order. Calibration IQ work
prep is CIQ-first: ADAS Map on each RO is treated as the governing calibration
requirement source, missing CIQ requirements are reconciled through CIQ's own
verified routine operator actions, and ADAS SI is checked only for the resulting
job requirements. Calibration and policy research deep-read later PDF
pages/sidebars and bounded same-host OEM web pages, while chat presentation stays
concise and conversation-first. Calibration IQ RO research remains on its existing
verified operator lane and inherits the same exact-vehicle deep ADAS PDF search.
Final policy and audit guards keep execution truthful and fail closed.
"""

from . import adas_si as _adas_si
from . import adas_ocr as _adas_ocr
from . import adas_identity_guard as _adas_identity_guard
from . import adas_calibration_depth as _adas_calibration_depth
from . import adas_calibration_identity as _adas_calibration_identity
from . import research_operator as _research_operator
from . import research_capture as _research_capture
from . import research_setup as _research_setup
from . import research_vault as _research_vault
from . import research_workflow as _research_workflow
from . import research_workflow_guard as _research_workflow_guard
from . import research_calibration_route as _research_calibration_route
from . import research_alldata_navigation as _research_alldata_navigation
from . import research_alldata_contract as _research_alldata_contract
from . import research_alldata_terms as _research_alldata_terms
from . import research_auto_acquire as _research_auto_acquire
from . import research_alldata_quick_reference as _research_alldata_quick_reference
from . import calibration_iq_work_prep as _calibration_iq_work_prep
from . import calibration_iq_work_prep_guards as _calibration_iq_work_prep_guards
from . import research_task_continuity as _research_task_continuity
from . import research_policy_depth as _research_policy_depth
from . import research_policy_compat as _research_policy_compat
from . import research_conversation as _research_conversation
from . import research_truth as _research_truth
from . import research_policy as _research_policy
from . import research_route_compat as _research_route_compat

_adas_ocr.install_class(_adas_si.AdasSI)
_adas_identity_guard.install(_adas_si)
_adas_calibration_depth.install(_adas_si)
_adas_calibration_identity.install(_adas_si)
_research_operator.install()
_research_route_compat.install()
_research_vault.install()
_research_capture.install()
_research_setup.install()
_research_workflow.install()
_research_workflow_guard.install()
_research_calibration_route.install()
_research_alldata_navigation.install()
_research_alldata_contract.install()
_research_alldata_terms.install()
_research_auto_acquire.install()
_research_alldata_quick_reference.install()
_calibration_iq_work_prep.install()
_calibration_iq_work_prep_guards.install()
_research_conversation.install()
_research_task_continuity.install()
_research_policy_depth.install()
_research_policy_compat.install()
_research_truth.install()
_research_policy.install()
