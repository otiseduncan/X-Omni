import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

async function cameraMotionClipPresentation() {
  const source = await readFile(
    new URL("../src/components/cards/Cards.jsx", import.meta.url),
    "utf8",
  );
  const start = source.indexOf("function cameraMotionClipPresentation");
  const end = source.indexOf("\n}\n\nfunction CameraMotionClipCard", start);
  assert.ok(start >= 0 && end > start, "camera clip presentation must remain independently testable");

  const sandbox = { Number };
  vm.runInNewContext(source.slice(start, end + 2), sandbox, {
    filename: "camera-motion-clip-presentation.js",
  });
  return sandbox.cameraMotionClipPresentation;
}

test("continuous DVR footage renders truthful labels without undefined frame metadata", async () => {
  const present = await cameraMotionClipPresentation();
  const result = present({
    source: "continuous_dvr",
    started_at_local: "Aug 29, 5:32 PM",
    ended_at_local: "Aug 29, 5:35 PM",
    partial: true,
    cached: true,
  });

  assert.equal(result.title, "Continuous DVR footage");
  assert.equal(result.descriptionFallback, "Requested continuous camera footage.");
  assert.equal(
    result.details,
    "Aug 29, 5:32 PM – Aug 29, 5:35 PM · continuous DVR · available portion · cached",
  );
  assert.doesNotMatch(result.details, /undefined|frames/);
});

test("stored motion clips retain their frame count while partial metadata stays clean", async () => {
  const present = await cameraMotionClipPresentation();

  assert.deepEqual(
    { ...present({
      source: "stored_frame_timelapse",
      started_at_local: "5:30 PM",
      ended_at_local: "5:31 PM",
      frame_count: 12,
    }) },
    {
      title: "Motion event clip",
      descriptionFallback: "No description available.",
      errorFallback: "This motion event's clip could not be assembled.",
      details: "5:30 PM – 5:31 PM · 12 frames",
    },
  );
  assert.equal(present({ started_at_local: "5:30 PM" }).details, "5:30 PM");
});

test("camera history exposes only the server-declared standalone DVR handoff", async () => {
  const [cards, styles] = await Promise.all([
    readFile(new URL("../src/components/cards/Cards.jsx", import.meta.url), "utf8"),
    readFile(new URL("../src/styles/app.css", import.meta.url), "utf8"),
  ]);
  assert.match(cards, /const dvrUrl = data\?\.dvr_url === "\/dvr" \? "\/dvr" : null/);
  assert.match(cards, /href=\{dvrUrl\}[\s\S]*target="_blank"[\s\S]*Open standalone DVR/);
  assert.equal(cards.match(/Open standalone DVR/g)?.length, 2);
  assert.match(styles, /\.camera-dvr-link \{[^}]*text-decoration: none/);
});

test("temporal DVR analysis card preserves observed versus unresolved evidence", async () => {
  const cards = await readFile(
    new URL("../src/components/cards/Cards.jsx", import.meta.url),
    "utf8",
  );
  const start = cards.indexOf("function CameraFootageAnalysisCard");
  const end = cards.indexOf("\n}\n\nfunction ImageGenerationStatusCard", start);
  assert.ok(start >= 0 && end > start, "temporal DVR analysis card must be present");
  const card = cards.slice(start, end + 2);
  assert.match(cards, /camera_footage_analysis:\s*CameraFootageAnalysisCard/);
  assert.match(card, /vehicle_movement_observation === "observed"/);
  assert.match(card, /not observed in sampled frames/);
  assert.match(card, /No absence-of-action conclusion is drawn from insufficient DVR samples/);
  assert.match(card, /<video[\s\S]*?src=\{data\.clip_url\}[\s\S]*?controls[\s\S]*?playsInline/);
});
