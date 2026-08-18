import { receiptState } from "./conversationTimeline.js";

const SHA256_RE = /^[0-9a-f]{64}$/;
const OFFICIAL_WAN_ASSETS = Object.freeze({
  "wan2.2_ti2v_5B_fp16.safetensors": Object.freeze({
    bytes: 9_999_658_848,
    sha256: "456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e",
  }),
  "umt5_xxl_fp8_e4m3fn_scaled.safetensors": Object.freeze({
    bytes: 6_735_906_897,
    sha256: "c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68",
  }),
  "wan2.2_vae.safetensors": Object.freeze({
    bytes: 1_409_400_960,
    sha256: "e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156",
  }),
});

export function generatedVideoUrl(digest) {
  return SHA256_RE.test(String(digest || ""))
    ? `/api/generated-videos/${digest}.mp4`
    : null;
}

export function generatedVideoPosterUrl(sourceDigest) {
  return SHA256_RE.test(String(sourceDigest || ""))
    ? `/api/generated-images/${sourceDigest}.png`
    : null;
}

/**
 * Turn only explicit Wan failure-state proof into operator-facing copy.
 * Exception names such as ConnectTimeout do not prove whether ComfyUI
 * accepted a prompt or whether Omni was unloaded, so they are deliberately
 * insufficient here.
 */
export function videoFailureDisclosure(data) {
  if (!data || typeof data !== "object" || Array.isArray(data)) return null;
  const generation = data.generation;
  const lifecycle = data.lifecycle;
  if (
    !generation ||
    typeof generation !== "object" ||
    Array.isArray(generation) ||
    !lifecycle ||
    typeof lifecycle !== "object" ||
    Array.isArray(lifecycle)
  ) {
    return null;
  }

  if (
    generation.submit_state === "not_attempted" &&
    generation.may_have_generated === false &&
    lifecycle.model_stopped === false
  ) {
    return {
      state: "not_submitted",
      title: "Video generation did not start",
      message: "The Wan request was not submitted, and Omni was not stopped.",
    };
  }

  if (
    generation.submit_state === "indeterminate" &&
    generation.may_have_generated === true
  ) {
    return {
      state: "indeterminate",
      title: "Video submission uncertain",
      message: "Wan may have begun generation, but submission was not confirmed. No video is displayed without a completed, verified result.",
    };
  }

  return null;
}

function sameProof(left, right) {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((value, index) => sameProof(value, right[index]));
  }
  if (!left || !right || typeof left !== "object" || typeof right !== "object") {
    return false;
  }
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  return leftKeys.length === rightKeys.length &&
    leftKeys.every((key, index) => key === rightKeys[index] && sameProof(left[key], right[key]));
}

function requiredFieldsMatch(data, result, expected) {
  return Object.entries(expected).every(
    ([key, value]) => data[key] === value && result[key] === value
  );
}

function officialWanAssetsMatch(assets) {
  if (!assets || typeof assets !== "object" || Array.isArray(assets)) return false;
  const filenames = Object.keys(OFFICIAL_WAN_ASSETS).sort();
  if (!sameProof(Object.keys(assets).sort(), filenames)) return false;
  return filenames.every((filename) => {
    const proof = assets[filename];
    const expected = OFFICIAL_WAN_ASSETS[filename];
    return proof &&
      typeof proof === "object" &&
      !Array.isArray(proof) &&
      sameProof(Object.keys(proof).sort(), ["bytes", "sha256", "verified"]) &&
      proof.verified === true &&
      proof.bytes === expected.bytes &&
      proof.sha256 === expected.sha256;
  });
}

function commonVideoProof(data, result, receipt) {
  const digest = String(data.sha256 || "");
  const sourceDigest = String(data.source_sha256 || "");
  const videoUrl = generatedVideoUrl(digest);
  const posterUrl = generatedVideoPosterUrl(sourceDigest);
  const duration = data.duration_seconds;
  const dimensionsValid = [data.width, data.height].every(
    (value) => Number.isInteger(value) && value >= 64 && value <= 4096 && value % 2 === 0
  );
  const commonTruth = {
    ok: true,
    status: "completed",
    executed: true,
    success: true,
    actual_video: true,
    verified: true,
    source_verified: true,
    mime_type: "video/mp4",
    codec: "h264",
    pixel_format: "yuv420p",
    fps: 24,
  };

  return Boolean(
    receiptState(receipt) === "succeeded" &&
    receipt.tool_name === "video_generate" &&
    videoUrl &&
    posterUrl &&
    requiredFieldsMatch(data, result, commonTruth) &&
    data.video_url === videoUrl &&
    data.target === videoUrl &&
    result.sha256 === digest &&
    result.source_sha256 === sourceDigest &&
    result.video_url === videoUrl &&
    result.target === videoUrl &&
    Number.isInteger(data.bytes) &&
    data.bytes > 0 &&
    Number.isInteger(duration) &&
    duration >= 2 &&
    duration <= 10 &&
    data.frame_count === duration * 24 &&
    dimensionsValid &&
    result.bytes === data.bytes &&
    result.duration_seconds === duration &&
    result.frame_count === data.frame_count &&
    result.width === data.width &&
    result.height === data.height
  );
}

function proceduralProofMatches(data, result) {
  const expected = {
    actual_generation: false,
    source_preserved: true,
    provider: "ffmpeg-exact-local",
    render_kind: "deterministic_exact_source_animation",
    profile: "hover_pulse",
  };
  const explicitMode = data.mode === "exact_source_animation" &&
    result.mode === "exact_source_animation" &&
    data.source_conditioned === false &&
    result.source_conditioned === false;
  // The first receipt-backed procedural renderer predated the explicit mode
  // and source_conditioned fields. Preserve only that exact historical shape;
  // all of its substantive provider, render, media, lifecycle, and receipt
  // proofs still have to match below.
  const legacyMode = data.mode === undefined &&
    result.mode === undefined &&
    data.source_conditioned === undefined &&
    result.source_conditioned === undefined;
  const lifecycle = data.lifecycle;
  return (explicitMode || legacyMode) &&
    requiredFieldsMatch(data, result, expected) &&
    lifecycle?.mode === "bounded_cpu_subprocess" &&
    lifecycle?.model_remained_available === true &&
    sameProof(lifecycle, result.lifecycle);
}

function generativeProofMatches(data, result) {
  const expected = {
    mode: "image_to_video",
    actual_generation: true,
    source_preserved: false,
    source_conditioned: true,
    provider: "comfyui-wan2.2-ti2v-5b-local",
    render_kind: "generative_image_to_video",
    model_id: "Wan2.2-TI2V-5B",
  };
  const lifecycle = data.lifecycle;
  const gpuIndices = lifecycle?.gpu_indices;
  return requiredFieldsMatch(data, result, expected) &&
    data.width === 704 &&
    data.height === 704 &&
    Number.isSafeInteger(data.seed) &&
    data.seed >= 0 &&
    result.seed === data.seed &&
    SHA256_RE.test(String(data.prompt_sha256 || "")) &&
    result.prompt_sha256 === data.prompt_sha256 &&
    officialWanAssetsMatch(data.model_assets) &&
    sameProof(data.model_assets, result.model_assets) &&
    lifecycle?.mode === "sequential_exclusive" &&
    lifecycle?.model_stopped === true &&
    lifecycle?.model_restored === true &&
    Array.isArray(gpuIndices) &&
    gpuIndices.length > 0 &&
    gpuIndices.every((index) => Number.isInteger(index) && index >= 0) &&
    sameProof(lifecycle, result.lifecycle);
}

/**
 * Require one exact successful video_generate receipt before exposing media.
 * The procedural and genuine I2V modes deliberately have disjoint proof
 * contracts; a partial I2V file can never fall back to procedural validation.
 */
export function videoReceiptMatches(data, receipt) {
  if (!data || typeof data !== "object" || !receipt || typeof receipt !== "object") {
    return false;
  }
  const result = receipt.result;
  if (!result || typeof result !== "object" || !commonVideoProof(data, result, receipt)) {
    return false;
  }
  if (data.mode === "exact_source_animation" || data.mode === undefined) {
    return proceduralProofMatches(data, result);
  }
  if (data.mode === "image_to_video") return generativeProofMatches(data, result);
  return false;
}

export function verifiedVideoMedia(data, receipt) {
  if (!videoReceiptMatches(data, receipt)) return null;
  return {
    src: generatedVideoUrl(data.sha256),
    poster: generatedVideoPosterUrl(data.source_sha256),
    filename: `x-omni-${data.sha256.slice(0, 12)}.mp4`,
  };
}
