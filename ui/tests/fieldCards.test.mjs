import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  CALIBRATION_COLLECTION_WARNING,
  calibrationCountLabel,
  calibrationPhaseBreakdown,
  calibrationSummaryScopeLabel,
  calibrationStatusTone,
} from "../src/lib/calibrationIqPresentation.js";

test("Calibration IQ status and phase presentation stays truthful", () => {
  assert.equal(calibrationStatusTone("Calibration Complete"), "done");
  assert.equal(calibrationStatusTone("No Calibration Required"), "done");
  assert.equal(calibrationStatusTone("  CALIBRATION COMPLETE  "), "done");
  assert.notEqual(calibrationStatusTone("Calibration Incomplete"), "done");
  assert.notEqual(calibrationStatusTone("Complete Prerequisites"), "done");
  assert.notEqual(calibrationStatusTone("Calibration Complete - Pending"), "done");
  assert.notEqual(calibrationStatusTone("No Calibration Required Review"), "done");
  assert.equal(calibrationStatusTone("Repair In Progress"), "active");
  assert.equal(calibrationStatusTone("Waiting On Prerequisites"), "warn");
  assert.equal(calibrationStatusTone("Ready for Calibration"), "ok");

  assert.equal(calibrationPhaseBreakdown(5, 3), "3 in Phase 5");
  assert.equal(calibrationPhaseBreakdown("Phase 6", 2), "2 in Phase 6");
  assert.equal(calibrationPhaseBreakdown("unspecified", 1), "1 no phase");
});

test("Calibration IQ cards expose summary, list, empty, error, and incomplete states", async () => {
  const cards = await readFile(
    new URL("../src/components/cards/FieldCards.jsx", import.meta.url),
    "utf8",
  );

  assert.match(cards, /calibration_iq_ros:\s*CalibrationRosCard/);
  assert.match(cards, /calibration_iq_summary:\s*CalibrationSummaryCard/);
  assert.match(cards, /calibration_iq_receipt:\s*CalibrationReceiptCard/);
  assert.equal((cards.match(/calibration_iq_summary:\s*CalibrationSummaryCard/g) || []).length, 1);
  assert.match(cards, /<div className="ciq-count">[\s\S]*?<strong>/);
  assert.match(cards, /calibrationPhaseBreakdown\(label, n\)/);
  assert.match(cards, /Showing \{data\.shown_count\} of \{totalLabel\}/);
  assert.match(cards, /No repair orders matched that filter/);
  assert.match(cards, /Calibration IQ is unavailable/);
  assert.match(cards, /data\.collection_capped[\s\S]*?CALIBRATION_COLLECTION_WARNING/);
  assert.match(cards, /finished \{data\.completed_count === 1 \? "order" : "orders"\} excluded/);
  assert.match(cards, /Calibration IQ — partially completed/);
  assert.match(cards, /data\?\.receipts/);
  assert.match(cards, /verification\?\.verified/);
  assert.match(cards, /duplicate absorbed/);
  assert.match(cards, /not executed/);
  assert.match(cards, /Missing documentation:/);

  assert.equal(calibrationCountLabel(59, false), "59");
  assert.equal(calibrationCountLabel(400, true), "at least 400");
  assert.equal(calibrationSummaryScopeLabel({ terminalOnly: true }), "finished repair orders");
  assert.equal(calibrationSummaryScopeLabel({ includeCompleted: true }), "repair orders");
  assert.equal(calibrationSummaryScopeLabel({}), "active repair orders");
  assert.equal(
    calibrationSummaryScopeLabel({ terminalOnly: true, collectionCapped: true }),
    "finished repair orders counted",
  );
  assert.match(CALIBRATION_COLLECTION_WARNING, /may be incomplete/i);
});

test("calibration_iq_work_prep is wired to a dedicated card, not silently dropped", async () => {
  const cards = await readFile(
    new URL("../src/components/cards/FieldCards.jsx", import.meta.url),
    "utf8",
  );

  // Registered in FIELD_CARDS -- otherwise a work-prep result would render
  // as nothing (conversationTimeline.js: "Unknown types safely render null").
  assert.match(cards, /calibration_iq_work_prep:\s*CalibrationWorkPrepCard/);
  assert.equal(
    (cards.match(/calibration_iq_work_prep:\s*CalibrationWorkPrepCard/g) || []).length,
    1,
  );

  // Every mode from calibration_iq_work_prep.py's TOOL_SCHEMAS enum must
  // route somewhere -- a missing case would fall through to whatever the
  // default renders, silently showing the wrong shape.
  assert.match(cards, /case "phase_list":/);
  assert.match(cards, /case "queue_next":/);
  assert.match(cards, /case "ro_requirements":/);

  // phase_list shares calibration_iq_read's exact result shape (see
  // calibration_iq_work_prep.py's _phase_list: `{"mode": "phase_list",
  // **result}` where result comes straight from read_repair_orders) --
  // it must reuse CalibrationRosCard rather than reinterpreting that shape.
  assert.match(cards, /function WorkPrepPhaseListCard[\s\S]*?<CalibrationRosCard/);

  // The default (week_readiness / phase_coverage) branch surfaces the
  // fields calibration_iq_work_prep.py's _week_readiness actually returns.
  assert.match(cards, /data\?\.exception_count/);
  assert.match(cards, /data\?\.queue_count/);
  assert.match(cards, /adas_map_verified_count/);
  assert.match(cards, /si_covered_count/);
  assert.match(cards, /repair_orders_truncated/);
  assert.match(cards, /r\.ready !== true/);
});

test("Calibration IQ card CSS protects 360, 390, and 430 pixel layouts", async () => {
  const styles = await readFile(
    new URL("../src/styles/field-cards.css", import.meta.url),
    "utf8",
  );
  const mobileMax = Number(styles.match(/@media \(max-width:\s*(430)px\)/)?.[1]);

  for (const width of [360, 390, 430]) {
    assert.ok(width <= mobileMax, `${width}px must be covered by the CIQ mobile rule`);
  }

  assert.match(styles, /\.field-card \.card-head\s*\{[\s\S]*?flex-wrap:\s*wrap;/);
  assert.match(styles, /\.field-meta\s*\{[\s\S]*?min-width:\s*0;[\s\S]*?max-width:\s*60%;[\s\S]*?overflow-wrap:\s*anywhere;/);
  assert.match(styles, /\.ciq-chips\s*\{[\s\S]*?flex-wrap:\s*wrap;[\s\S]*?min-width:\s*0;/);
  assert.match(styles, /\.ciq-chips \.ro-pill\s*\{[\s\S]*?max-width:\s*100%;[\s\S]*?white-space:\s*normal;[\s\S]*?overflow-wrap:\s*anywhere;/);
  assert.match(styles, /\.ro-pill\.done\s*\{/);
  assert.match(styles, /\.ciq-incomplete\s*\{[\s\S]*?color:\s*var\(--warning\);/);
  assert.match(styles, /@media \(max-width:\s*430px\)[\s\S]*?\.ciq-count\s*\{[\s\S]*?flex-wrap:\s*wrap;/);
});
