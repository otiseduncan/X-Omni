import assert from "node:assert/strict";
import test from "node:test";

import {
  mergeTimelines,
  receiptDisplayState,
  receiptMatchesArtifact,
  receiptState,
  receiptUpdateFromArtifact,
  terminalMediaWorkload,
  timelineFromHistory,
  upsertTimelineItem,
  updateApproval,
  websiteRevision,
} from "../src/lib/conversationTimeline.js";

const request = {
  type: "approval_request",
  data: {
    id: "approve-1",
    tool: "write_file",
    summary: "Write config",
    args: { path: "demo.txt" },
    status: "pending",
  },
};

const receipt = {
  id: "receipt-1",
  approval_id: "approve-1",
  tool_name: "write_file",
  status: "succeeded",
  executed: true,
  success: true,
};

test("restores messages, artifacts, and one receipt-backed approval card", () => {
  const timeline = timelineFromHistory([
    { id: 1, role: "user", content: "hello", artifacts: [] },
    { id: 2, role: "assistant", content: "", artifacts: [request] },
    {
      id: 3,
      role: "assistant",
      content: "done",
      worker_used: "omni",
      artifacts: [{ type: "execution_receipt", data: receipt }],
    },
  ]);

  assert.equal(timeline.filter((item) => item.kind === "approval").length, 1);
  assert.equal(timeline.find((item) => item.kind === "approval").status, "succeeded");
  assert.equal(timeline.find((item) => item.kind === "assistant").worker, "omni");
});

test("never treats approval or a success label without receipt proof as executed", () => {
  assert.equal(receiptState({ status: "succeeded", executed: false, success: true }), null);
  assert.equal(receiptState({ status: "succeeded", executed: true, success: false }), null);
  assert.equal(receiptState({ status: "succeeded", executed: true, success: true }), "succeeded");

  const pending = timelineFromHistory([{ id: 2, role: "assistant", content: "", artifacts: [request] }]);
  const claimed = updateApproval(pending, "approve-1", { status: "succeeded" });
  assert.notEqual(claimed[0].status, "succeeded");
});

test("only terminal media receipts settle an external workload", () => {
  const imageReceipt = {
    tool_name: "image_generate",
    status: "failed",
    executed: true,
    success: false,
  };
  const videoReceipt = {
    tool_name: "video_generate",
    status: "succeeded",
    executed: true,
    success: true,
  };
  const uncertainVideoReceipt = {
    tool_name: "video_generate",
    status: "failed",
    executed: false,
    success: false,
    result: { execution_state: "indeterminate", may_have_executed: true },
  };

  assert.equal(terminalMediaWorkload(imageReceipt), "image_generation");
  assert.equal(terminalMediaWorkload(videoReceipt), "video_generation");
  assert.equal(terminalMediaWorkload(uncertainVideoReceipt), "video_generation");
  assert.equal(terminalMediaWorkload({ ...videoReceipt, status: "executing" }), null);
  assert.equal(terminalMediaWorkload({ ...videoReceipt, status: "approved" }), null);
  assert.equal(terminalMediaWorkload({ ...videoReceipt, status: "pending" }), null);
  assert.equal(terminalMediaWorkload({ ...videoReceipt, tool_name: "run_powershell" }), null);
  assert.equal(terminalMediaWorkload(null), null);
});

test("applies the WebSocket executing then receipt lifecycle", () => {
  const pending = timelineFromHistory([{ id: 2, role: "assistant", content: "", artifacts: [request] }]);
  const executing = updateApproval(pending, "approve-1", { status: "executing" });
  assert.equal(executing[0].status, "executing");

  const succeeded = updateApproval(executing, "approve-1", {
    status: "succeeded",
    receipt,
  });
  assert.equal(succeeded[0].status, "succeeded");
  assert.equal(succeeded[0].receipt.id, "receipt-1");
});

test("folds live receipt artifacts into the approval and labels interrupted execution indeterminate", () => {
  const pending = timelineFromHistory([{ id: 2, role: "assistant", content: "", artifacts: [request] }]);
  const update = receiptUpdateFromArtifact({ type: "execution_receipt", data: receipt });
  const succeeded = updateApproval(pending, update.id, update);
  assert.equal(succeeded.length, 1);
  assert.equal(succeeded[0].status, "succeeded");
  assert.equal(succeeded[0].receipt.id, "receipt-1");

  const interrupted = {
    ...receipt,
    status: "failed",
    executed: false,
    success: false,
    execution_state: "indeterminate",
    may_have_executed: true,
    outcome_message: "Outcome unknown; it was not run again.",
  };
  assert.equal(receiptState(interrupted), "failed");
  assert.equal(receiptDisplayState(interrupted), "indeterminate");
  const restored = timelineFromHistory([{
    id: 3,
    role: "assistant",
    content: "",
    artifacts: [{ type: "execution_receipt", data: interrupted }],
  }]);
  assert.equal(restored[0].status, "indeterminate");
  assert.equal(restored[0].approval.may_have_executed, true);
});

test("restored PowerShell result carries its matching receipt for verified disclosure", () => {
  const result = {
    command: "Write-Output ok",
    exit_code: 0,
    timed_out: false,
    stdout: "ok",
    stderr: "",
    stdout_bytes: 2,
    stderr_bytes: 0,
  };
  const shellReceipt = { ...receipt, tool_name: "run_powershell", result };
  const timeline = timelineFromHistory([{
    id: 4,
    role: "assistant",
    content: "done",
    artifacts: [
      { type: "execution_receipt", data: shellReceipt },
      { type: "shell_result", data: result },
    ],
  }]);
  const shell = timeline.find((item) => item.kind === "artifact");
  assert.equal(shell.artifact.type, "shell_result");
  assert.equal(shell.artifact.receipt.id, "receipt-1");
});

test("live and restored generated images carry only the image_generate receipt", () => {
  const digest = "a".repeat(64);
  const imageResult = {
    status: "completed",
    verified: true,
    actual_generation: true,
    sha256: digest,
    image_url: `/api/generated-images/${digest}.png`,
    target: `/api/generated-images/${digest}.png`,
    lifecycle: { model_restored: true },
  };
  const imageReceipt = {
    ...receipt,
    tool_name: "image_generate",
    result: imageResult,
  };
  assert.equal(receiptMatchesArtifact(imageReceipt, "generated_image"), true);
  assert.equal(receiptMatchesArtifact(imageReceipt, "shell_result"), false);

  const timeline = timelineFromHistory([{
    id: 5,
    role: "assistant",
    content: "",
    artifacts: [
      { type: "execution_receipt", data: imageReceipt },
      { type: "generated_image", data: imageResult },
    ],
  }]);
  const image = timeline.find((item) => item.kind === "artifact");
  assert.equal(image.artifact.type, "generated_image");
  assert.equal(image.artifact.receipt.tool_name, "image_generate");
  assert.equal(image.artifact.receipt.result.sha256, digest);
});

test("live and restored generated videos carry only the video_generate receipt", () => {
  const digest = "c".repeat(64);
  const videoResult = {
    status: "completed",
    verified: true,
    actual_video: true,
    actual_generation: false,
    sha256: digest,
    video_url: `/api/generated-videos/${digest}.mp4`,
    target: `/api/generated-videos/${digest}.mp4`,
  };
  const videoReceipt = {
    ...receipt,
    tool_name: "video_generate",
    result: videoResult,
  };
  assert.equal(receiptMatchesArtifact(videoReceipt, "generated_video"), true);
  assert.equal(receiptMatchesArtifact(videoReceipt, "generated_image"), false);

  const timeline = timelineFromHistory([{
    id: 6,
    role: "assistant",
    content: "",
    artifacts: [
      { type: "execution_receipt", data: videoReceipt },
      { type: "generated_video", data: videoResult },
    ],
  }]);
  const video = timeline.find((item) => item.kind === "artifact");
  assert.equal(video.artifact.type, "generated_video");
  assert.equal(video.artifact.receipt.tool_name, "video_generate");
  assert.equal(video.artifact.receipt.result.sha256, digest);
});

test("folds terminal denial and failure receipts into the request", () => {
  for (const [status, expected] of [["denied", "denied"], ["failed", "failed"]]) {
    const timeline = timelineFromHistory([
      { id: 2, role: "assistant", content: "", artifacts: [request] },
      {
        id: 3,
        role: "assistant",
        content: "",
        artifacts: [{
          type: "execution_receipt",
          data: { ...receipt, status, executed: status === "failed", success: false },
        }],
      },
    ]);
    assert.equal(timeline[0].status, expected);
  }
});

test("reconciliation removes optimistic message and artifact duplicates", () => {
  const authoritative = timelineFromHistory([
    { id: 9, role: "user", content: "same message", artifacts: [] },
    { id: 10, role: "assistant", content: "", artifacts: [{ type: "weather", data: { ok: true } }] },
  ]);
  const live = [
    { kind: "user", key: "client:1", text: "same message" },
    { kind: "artifact", key: "client:2", artifact: { type: "weather", data: { ok: true } } },
  ];
  assert.equal(mergeTimelines(authoritative, live).length, 2);
});

test("unknown artifacts remain harmless registry entries", () => {
  const timeline = timelineFromHistory([
    { id: 12, role: "assistant", content: "ok", artifacts: [{ type: "future_type", data: {} }] },
  ]);
  assert.equal(timeline[1].kind, "artifact");
  assert.equal(timeline[1].artifact.type, "future_type");
});

test("successful website revisions supersede one visible card while preserving its key", () => {
  const firstHash = "a".repeat(64);
  const secondHash = "b".repeat(64);
  const timeline = timelineFromHistory([
    {
      id: 20,
      role: "assistant",
      content: "Original",
      artifacts: [{
        type: "website_preview",
        data: {
          ok: true,
          status: "generated_preview",
          website_id: "tim-towing",
          sha256: firstHash,
          html: "<!doctype html><title>Original</title>",
        },
      }],
    },
    {
      id: 21,
      role: "assistant",
      content: "Updated",
      artifacts: [{
        type: "website_preview",
        data: {
          ok: true,
          status: "generated_preview",
          website_id: "tim-towing",
          parent_sha256: firstHash,
          sha256: secondHash,
          html: "<!doctype html><title>Updated</title>",
        },
      }],
    },
  ]);

  const websites = timeline.filter(
    (item) => item.kind === "artifact" && item.artifact.type === "website_preview"
  );
  assert.equal(websites.length, 1);
  assert.equal(websites[0].key, "artifact:20:0");
  assert.equal(websites[0].artifact.data.sha256, secondHash);
  assert.equal(websites[0].artifact.data.parent_sha256, firstHash);
});

test("a parent hash can coalesce a live revision and failed attempts stay visible", () => {
  const firstHash = "c".repeat(64);
  const secondHash = "d".repeat(64);
  const original = {
    kind: "artifact",
    key: "artifact:30:0",
    artifact: {
      type: "website_preview",
      data: { ok: true, status: "generated_preview", sha256: firstHash, html: "<html>one</html>" },
    },
  };
  const update = {
    kind: "artifact",
    key: "client:update",
    artifact: {
      type: "website_preview",
      data: {
        ok: true,
        status: "generated_preview",
        parent_sha256: firstHash,
        sha256: secondHash,
        html: "<html>two</html>",
      },
    },
  };
  const failed = {
    kind: "artifact",
    key: "client:failed",
    artifact: {
      type: "website_preview",
      data: {
        ok: false,
        status: "timed_out",
        website_id: "tim-towing",
        parent_sha256: secondHash,
        message: "The update timed out.",
      },
    },
  };

  const updated = upsertTimelineItem([original], update);
  assert.equal(updated.length, 1);
  assert.equal(updated[0].key, original.key);
  assert.equal(updated[0].artifact.data.sha256, secondHash);
  assert.equal(websiteRevision(failed), null);
  const withFailure = upsertTimelineItem(updated, failed);
  assert.equal(withFailure.length, 2);
  assert.equal(withFailure[1].artifact.data.message, "The update timed out.");
});

test("a stale parent cannot replace a newer visible website revision", () => {
  const firstHash = "e".repeat(64);
  const currentHash = "f".repeat(64);
  const current = {
    kind: "artifact",
    key: "artifact:40:0",
    artifact: {
      type: "website_preview",
      data: {
        ok: true,
        status: "generated_preview",
        website_id: "site-1",
        parent_sha256: firstHash,
        sha256: currentHash,
        html: "<html>current</html>",
      },
    },
  };
  const stale = {
    kind: "artifact",
    key: "client:stale",
    artifact: {
      type: "website_preview",
      data: {
        ok: true,
        status: "generated_preview",
        website_id: "site-1",
        parent_sha256: firstHash,
        sha256: "1".repeat(64),
        html: "<html>stale branch</html>",
      },
    },
  };
  const items = upsertTimelineItem([current], stale);
  assert.equal(items.length, 2);
  assert.equal(items[0].artifact.data.sha256, currentHash);
});
