"""Execution-only wiring for X Omni service capabilities.

The conversation orchestrator and Qwen own ordinary-language interpretation and
tool selection.  Installers in this package may add schemas, handlers, HTTP
routes, source adapters, OCR, identity checks, provenance checks, and execution
guards; they must not inspect a user turn to pre-route it or manufacture business
arguments from prose.

ADAS SI keeps one shared OCR/search implementation.  Calibration IQ keeps its
authorization, idempotency, optimistic-concurrency, receipt, and reread gates.
ALLDATA remains a licensed, vehicle-first operator with interactive human access.
ScrapeX remains a loopback-only ADAS Map acquisition service.  Durable subject
state is injected as trusted context and the model resolves follow-up language.
"""

from . import adas_si as _adas_si
from . import adas_ocr as _adas_ocr
from . import adas_identity_guard as _adas_identity_guard
from . import adas_calibration_depth as _adas_calibration_depth
from . import adas_calibration_identity as _adas_calibration_identity
from . import adas_si_research as _adas_si_research
from . import adas_si_research_identity as _adas_si_research_identity
from . import adas_si_research_adas_map_vin_repair as _adas_si_research_adas_map_vin_repair
from . import adas_si_research_source_cascade as _adas_si_research_source_cascade
from . import calibration_iq as _calibration_iq
from . import calibration_iq_endpoint_discovery as _calibration_iq_endpoint_discovery
from . import calibration_iq_data_plane_guard as _calibration_iq_data_plane_guard
from . import calibration_iq_projection_guard as _calibration_iq_projection_guard
from . import conversation_contract_guard as _conversation_contract_guard
from . import scrapex as _scrapex
from . import scrapex_navigator_runtime as _scrapex_navigator_runtime
from . import research_navigator_agent as _research_navigator_agent
from . import research_bottom_decision_guard as _research_bottom_decision_guard
from . import research_navigator_tool_repair as _research_navigator_tool_repair
from . import research_semantic_review as _research_semantic_review
from . import research_semantic_system_guard as _research_semantic_system_guard
from . import research_objective_match_guard as _research_objective_match_guard
from . import research_navigator_vehicle_anchor as _research_navigator_vehicle_anchor
from . import research_dependency_budget_extension as _research_dependency_budget_extension
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
_adas_si_research_identity.install(_adas_si_research)
_adas_si_research_adas_map_vin_repair.install(_adas_si_research)
# SI research is intentionally VIN-bound. No year/make/model fallback is installed.
# If an older RO has an attached ADAS Map but no stored CIQ VIN, the adapter
# above may restore only the VIN already proven by that exact ScrapeX map result.
_calibration_iq_endpoint_discovery.install(_calibration_iq)
_calibration_iq_data_plane_guard.install(_calibration_iq)
_calibration_iq_projection_guard.install(_calibration_iq)
_conversation_contract_guard.install()
_scrapex_navigator_runtime.install(_scrapex)
_research_bottom_decision_guard.install(_research_navigator_agent, _scrapex)
_research_navigator_tool_repair.install(_research_navigator_agent)
_research_semantic_system_guard.install(_research_semantic_review, _research_navigator_agent)
_research_objective_match_guard.install(_research_semantic_review, _research_navigator_agent)
_research_navigator_vehicle_anchor.install(_research_navigator_agent)
_research_dependency_budget_extension.install(_research_navigator_agent)
_adas_si_research_source_cascade.install(_adas_si_research)
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
