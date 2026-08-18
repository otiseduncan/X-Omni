import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  generatedVideoPosterUrl,
  generatedVideoUrl,
  verifiedVideoMedia,
  videoFailureDisclosure,
  videoReceiptMatches,
} from "../src/lib/videoArtifact.js";

const OFFICIAL_ASSETS = {
  "wan2.2_ti2v_5B_fp16.safetensors": {
    verified: true,
    bytes: 9_999_658_848,
    sha256: "456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e",
  },
  "umt5_xxl_fp8_e4m3fn_scaled.safetensors": {
    verified: true,
    bytes: 6_735_906_897,
    sha256: "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
  },
  "wan2.2_vae.safetensors": {
    verified: true,
    bytes: 1_409_400_960,
    sha256: "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156",
  },
};

function verifiedPair() {
  const digest = "a".repeat(64);
  const sourceDigest = "b".repeat(64);
  const data = {
    ok: true,
    status: "completed",
    executed: true,
    success: true,
    actual_video: true,
    actual_generation: false,
    verified: true,
    source_preserved: true,
    source_conditioned: false,
    provider: "ffmpeg-exact-local",
    render_kind: "deterministic_exact_source_animation",
    source_verified: true,
    mime_type: "video/mp4",
    codec: "h264",
    pixel_format: "yuv420p",
    profile: "hover_pulse",
    mode: "exact_source_animation",
    fps: 24,
    duration_seconds: 10,
    frame_count: 240,
    width: 1024,
    height: 1024,
    bytes: 1_234_567,
    sha256: digest,
    source_sha256: sourceDigest,
    video_url: `/api/generated-videos/${digest}.mp4`,
    target: `/api/generated-videos/${digest}.mp4`,
    lifecycle: {
      mode: "bounded_cpu_subprocess",
      model_remained_available: true,
    },
  };
  return {
    data,
    receipt: {
      id: "video-receipt-1",
      approval_id: "video-approval-1",
      tool_name: "video_generate",
      status: "succeeded",
      executed: true,
      success: true,
      result: structuredClone(data),
    },
  };
}

function verifiedGenerativePair() {
  const digest = "c".repeat(64);
  const sourceDigest = "d".repeat(64);
  const data = {
    ok: true,
    status: "completed",
    executed: true,
    success: true,
    actual_video: true,
    actual_generation: true,
    verified: true,
    source_preserved: false,
    source_conditioned: true,
    source_verified: true,
    provider: "comfyui-wan2.2-ti2v-5b-local",
    render_kind: "generative_image_to_video",
    mode: "image_to_video",
    model_id: "Wan2.2-TI2V-5B",
    model_assets: structuredClone(OFFICIAL_ASSETS),
    seed: 77,
    prompt_sha256: "e".repeat(64),
    mime_type: "video/mp4",
    codec: "h264",
    pixel_format: "yuv420p",
    fps: 24,
    duration_seconds: 10,
    frame_count: 240,
    width: 704,
    height: 704,
    bytes: 2_345_678,
    sha256: digest,
    source_sha256: sourceDigest,
    video_url: `/api/generated-videos/${digest}.mp4`,
    target: `/api/generated-videos/${digest}.mp4`,
    lifecycle: {
      mode: "sequential_exclusive",
      model_stopped: true,
      model_restored: true,
      gpu_indices: [0, 1],
      previous_worker: "omni",
      external_runtime: "spawned",
    },
  };
  return {
    data,
    receipt: {
      id: "video-receipt-wan-1",
      approval_id: "video-approval-wan-1",
      tool_name: "video_generate",
      status: "succeeded",
      executed: true,
      success: true,
      result: structuredClone(data),
    },
  };
}

test("generated video media is content-addressed and strictly receipt-bound", () => {
  const { data, receipt } = verifiedPair();

  assert.equal(generatedVideoUrl(data.sha256), data.video_url);
  assert.equal(
    generatedVideoPosterUrl(data.source_sha256),
    `/api/generated-images/${data.source_sha256}.png`
  );
  assert.equal(videoReceiptMatches(data, receipt), true);
  assert.deepEqual(verifiedVideoMedia(data, receipt), {
    src: data.video_url,
    poster: `/api/generated-images/${data.source_sha256}.png`,
    filename: `x-omni-${data.sha256.slice(0, 12)}.mp4`,
  });

  const legacyData = structuredClone(data);
  const legacyReceipt = structuredClone(receipt);
  delete legacyData.mode;
  delete legacyData.source_conditioned;
  delete legacyReceipt.result.mode;
  delete legacyReceipt.result.source_conditioned;
  assert.equal(videoReceiptMatches(legacyData, legacyReceipt), true);
  assert.equal(
    verifiedVideoMedia(legacyData, legacyReceipt)?.src,
    legacyData.video_url
  );
});

test("procedural video rejects unproved, cross-tool, and result-divergent claims", () => {
  const { data, receipt } = verifiedPair();
  const variants = [
    [data, null],
    [data, { ...receipt, tool_name: "image_generate" }],
    [{ ...data, video_url: "https://localhost:8080/video.mp4" }, receipt],
    [{ ...data, target: "/api/generated-videos/other.mp4" }, receipt],
    [{ ...data, actual_generation: true }, receipt],
    [{ ...data, source_verified: false }, receipt],
    [{ ...data, codec: "hevc" }, receipt],
    [{ ...data, pixel_format: "yuv444p" }, receipt],
    [{ ...data, profile: "unapproved" }, receipt],
    [{ ...data, frame_count: 239 }, receipt],
    [{ ...data, bytes: 0 }, receipt],
    [{ ...data, lifecycle: { ...data.lifecycle, model_remained_available: false } }, receipt],
    [{ ...data, mode: undefined }, receipt],
    [{ ...data, source_conditioned: undefined }, receipt],
    [data, { ...receipt, result: { ...receipt.result, source_sha256: "c".repeat(64) } }],
  ];

  for (const [candidate, candidateReceipt] of variants) {
    assert.equal(videoReceiptMatches(candidate, candidateReceipt), false);
    assert.equal(verifiedVideoMedia(candidate, candidateReceipt), null);
  }
  assert.equal(generatedVideoUrl("../escape"), null);
  assert.equal(generatedVideoPosterUrl("not-a-digest"), null);
});

test("true Wan image-to-video accepts only exact official and restored-model proof", () => {
  const { data, receipt } = verifiedGenerativePair();
  assert.equal(videoReceiptMatches(data, receipt), true);
  assert.deepEqual(verifiedVideoMedia(data, receipt), {
    src: data.video_url,
    poster: `/api/generated-images/${data.source_sha256}.png`,
    filename: `x-omni-${data.sha256.slice(0, 12)}.mp4`,
  });

  const badAsset = structuredClone(data);
  badAsset.model_assets["wan2.2_vae.safetensors"].sha256 = "0".repeat(64);
  const extraAssetField = structuredClone(data);
  extraAssetField.model_assets["wan2.2_vae.safetensors"].path = "untrusted";
  const partialReceipt = {
    ...receipt,
    status: "failed",
    executed: true,
    success: false,
  };
  const receiptAssetMismatch = structuredClone(receipt);
  receiptAssetMismatch.result.model_assets["wan2.2_vae.safetensors"].sha256 = "0".repeat(64);
  const receiptLifecycleMismatch = structuredClone(receipt);
  receiptLifecycleMismatch.result.lifecycle.gpu_indices = [1, 0];
  const variants = [
    [{ ...data, provider: "unproved-provider" }, receipt],
    [{ ...data, render_kind: "deterministic_exact_source_animation" }, receipt],
    [{ ...data, model_id: "invented-model" }, receipt],
    [{ ...data, actual_generation: false }, receipt],
    [{ ...data, source_conditioned: false }, receipt],
    [{ ...data, source_preserved: true }, receipt],
    [{ ...data, source_verified: false }, receipt],
    [{ ...data, width: 1024 }, receipt],
    [{ ...data, height: 1024 }, receipt],
    [{ ...data, seed: Number.MAX_SAFE_INTEGER + 1 }, receipt],
    [{ ...data, prompt_sha256: "not-a-digest" }, receipt],
    [badAsset, receipt],
    [extraAssetField, receipt],
    [{ ...data, lifecycle: { ...data.lifecycle, model_stopped: false } }, receipt],
    [{ ...data, lifecycle: { ...data.lifecycle, model_restored: false } }, receipt],
    [{ ...data, lifecycle: { ...data.lifecycle, gpu_indices: [] } }, receipt],
    [data, { ...receipt, result: { ...receipt.result, seed: 78 } }],
    [data, { ...receipt, result: { ...receipt.result, prompt_sha256: "f".repeat(64) } }],
    [data, receiptAssetMismatch],
    [data, receiptLifecycleMismatch],
    [data, partialReceipt],
  ];

  for (const [candidate, candidateReceipt] of variants) {
    assert.equal(videoReceiptMatches(candidate, candidateReceipt), false);
    assert.equal(verifiedVideoMedia(candidate, candidateReceipt), null);
  }
});

test("Wan failure disclosures require explicit submission and model-stop proof", () => {
  const notSubmitted = {
    ok: false,
    status: "error",
    lifecycle: {
      mode: "sequential_exclusive",
      model_stop_attempted: true,
      model_stopped: false,
      model_restore_required: false,
      model_restored: null,
      external_runtime: "not_started",
    },
    generation: {
      submit_state: "not_attempted",
      prompt_id_known: false,
      prompt_cancelled: null,
      may_have_generated: false,
      may_have_surviving_output: false,
      output_removed: true,
    },
  };
  assert.deepEqual(videoFailureDisclosure(notSubmitted), {
    state: "not_submitted",
    title: "Video generation did not start",
    message: "The Wan request was not submitted, and Omni was not stopped.",
  });

  const indeterminate = structuredClone(notSubmitted);
  indeterminate.lifecycle.model_stopped = true;
  indeterminate.lifecycle.model_restore_required = true;
  indeterminate.generation.submit_state = "indeterminate";
  indeterminate.generation.may_have_generated = true;
  indeterminate.generation.may_have_surviving_output = null;
  assert.deepEqual(videoFailureDisclosure(indeterminate), {
    state: "indeterminate",
    title: "Video submission uncertain",
    message: "Wan may have begun generation, but submission was not confirmed. No video is displayed without a completed, verified result.",
  });

  for (const candidate of [
    null,
    {},
    { ...notSubmitted, generation: { ...notSubmitted.generation, may_have_generated: true } },
    { ...notSubmitted, lifecycle: { ...notSubmitted.lifecycle, model_stopped: true } },
    { ...indeterminate, generation: { ...indeterminate.generation, may_have_generated: false } },
    { message: "ConnectTimeout" },
  ]) {
    assert.equal(videoFailureDisclosure(candidate), null);
  }
});

test("failed and indeterminate Wan results never expose video media", () => {
  const { data, receipt } = verifiedGenerativePair();
  const failureStates = [
    {
      lifecycle: { model_stopped: false },
      generation: { submit_state: "not_attempted", may_have_generated: false },
    },
    {
      lifecycle: { model_stopped: true },
      generation: { submit_state: "indeterminate", may_have_generated: true },
    },
  ];

  for (const failureState of failureStates) {
    const failedData = { ...data, ...failureState, ok: false, status: "error", success: false };
    const failedReceipt = {
      ...receipt,
      status: "failed",
      success: false,
      result: structuredClone(failedData),
    };
    assert.equal(videoReceiptMatches(failedData, failedReceipt), false);
    assert.equal(verifiedVideoMedia(failedData, failedReceipt), null);
  }
});

test("video cards stay inline, operator-controlled, honest, and fail closed", async () => {
  const cards = await readFile(new URL("../src/components/cards/Cards.jsx", import.meta.url), "utf8");
  const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
  const timeline = await readFile(new URL("../src/lib/conversationTimeline.js", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/styles/app.css", import.meta.url), "utf8");
  const avatar = await readFile(new URL("../src/components/Avatar.jsx", import.meta.url), "utf8");
  const videoCard = cards.slice(
    cards.indexOf("function GeneratedVideoCard"),
    cards.indexOf("function VideoGenerationStatusCard")
  );

  assert.match(cards, /generated_video:\s*GeneratedVideoCard/);
  assert.match(cards, /video_generation_status:\s*VideoGenerationStatusCard/);
  assert.match(cards, /const failure = videoFailureDisclosure\(data\)/);
  assert.match(cards, /const title = failure\?\.title/);
  assert.match(cards, /failure\?\.message \|\| displayText/);
  assert.match(cards, /!failure && generativeAvailable/);
  assert.match(cards, /!failure && proceduralAvailable/);
  assert.match(videoCard, /<video[\s\S]*?src=\{media\.src\}[\s\S]*?poster=\{media\.poster\}[\s\S]*?controls[\s\S]*?playsInline[\s\S]*?preload="metadata"/);
  assert.doesNotMatch(videoCard, /autoPlay/);
  assert.match(videoCard, /onError=\{\(\) => setVideoLoadFailed\(true\)\}/);
  assert.match(videoCard, /videoLoadFailed \? \([\s\S]*?generated-video-load-error[\s\S]*?: \([\s\S]*?<video/);
  assert.match(videoCard, /!videoLoadFailed && \([\s\S]*?Download verified MP4/);
  assert.match(videoCard, /This is not generative video/);
  assert.match(videoCard, /AI-generated source-conditioned video/);
  assert.match(videoCard, /apparent 3D\/depth movement/);
  assert.match(videoCard, /not a reusable 3D mesh and is not pixel-exact/);
  assert.match(cards, /Procedural mode applies deterministic hover-and-pulse effects and is not AI-generated image-to-video/);
  assert.match(cards, /data\.modes\?\.image_to_video\?\.generation_available === true/);
  assert.match(cards, /Generative video and procedural animation available/);
  assert.doesNotMatch(cards, /href=\{data\?\.video_url\}|src=\{data\?\.video_url\}/);

  assert.match(timeline, /type === "generated_video"\) return "video_generate"/);
  assert.match(app, /"generated_video",[\s\S]{0,100}"video_generation_status"/);
  assert.match(app, /externalWorkload === "video_generation"/);
  assert.match(app, /conversation model may temporarily unload, and any unload must be verified restored/);
  assert.match(app, /success is shown only after the required runtime and model-restoration proofs pass/);
  assert.match(avatar, /externalWorkload === "video_generation"\) caption = "rendering video"/);
  assert.match(styles, /\.generated-video\s*\{[\s\S]*?width:\s*100%;[\s\S]*?height:\s*auto;[\s\S]*?object-fit:\s*contain;/);
  assert.match(styles, /\.generated-video-load-error\s*\{[\s\S]*?border:/);
  assert.match(styles, /\.generated-video-card\.is-generative\s*\{[\s\S]*?border-color:/);
});
