import assert from "node:assert/strict";
import test from "node:test";

import {
  copyGeneratedHtml,
  downloadGeneratedHtml,
  persistWebsiteView,
  restoredWebsiteView,
  websiteArtifactIdentity,
  websiteViewStorageKey,
  websiteHtmlFilename,
} from "../src/lib/websiteArtifact.js";
import { readFile } from "node:fs/promises";

test("copy generated HTML writes the exact source through the Clipboard API", async () => {
  const writes = [];
  const html = "<!doctype html><html><body><h1>Tim's Towing</h1></body></html>";

  const copied = await copyGeneratedHtml(html, {
    clipboard: { writeText: async (value) => writes.push(value) },
  });

  assert.equal(copied, html.length);
  assert.deepEqual(writes, [html]);
  await assert.rejects(copyGeneratedHtml(html, { clipboard: null }), /Clipboard access/);
});

test("download generated HTML uses a temporary Blob URL and always schedules revocation", () => {
  const html = "<!doctype html><title>Safe preview</title>";
  const events = [];
  const cleanup = [];
  let generatedBlob = null;
  const anchor = {
    href: "",
    download: "",
    rel: "",
    hidden: false,
    click: () => events.push("clicked"),
    remove: () => events.push("removed"),
  };
  const documentRef = {
    body: { appendChild: (node) => events.push(node === anchor ? "appended" : "wrong-node") },
    createElement: (tag) => {
      assert.equal(tag, "a");
      return anchor;
    },
  };
  const urlApi = {
    createObjectURL: (blob) => {
      generatedBlob = blob;
      events.push("created");
      return "blob:x-omni-preview";
    },
    revokeObjectURL: (url) => events.push(`revoked:${url}`),
  };

  const result = downloadGeneratedHtml(html, "Tim's Towing & Repair", {
    documentRef,
    urlApi,
    deferCleanup: (callback) => cleanup.push(callback),
  });

  assert.equal(result.filename, "tim-s-towing-repair.html");
  assert.equal(result.bytes, generatedBlob.size);
  assert.equal(generatedBlob.type, "text/html;charset=utf-8");
  assert.equal(anchor.href, "blob:x-omni-preview");
  assert.equal(anchor.download, result.filename);
  assert.equal(anchor.rel, "noopener");
  assert.deepEqual(events, ["created", "appended", "clicked", "removed"]);
  assert.equal(cleanup.length, 1);
  cleanup[0]();
  assert.equal(events.at(-1), "revoked:blob:x-omni-preview");
});

test("HTML download filenames are bounded and filesystem-neutral", () => {
  assert.equal(websiteHtmlFilename("  Café / Demo: v1  "), "cafe-demo-v1.html");
  assert.equal(websiteHtmlFilename("***"), "generated-website.html");
  assert.ok(websiteHtmlFilename("x".repeat(200)).length <= 69);
});

test("website artifact downloads never open a popup or retain object URLs", async () => {
  const source = await readFile(new URL("../src/lib/websiteArtifact.js", import.meta.url), "utf8");
  assert.match(source, /URL|urlApi/);
  assert.match(source, /revokeObjectURL\(objectUrl\)/);
  assert.doesNotMatch(source, /window\.open|target\s*=\s*["']_blank/);
});

test("website view survives a live-to-restored card remount without storing generated content", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
  const artifact = {
    title: "Private demo title",
    html: "<!doctype html><title>must not be stored</title>",
    sha256: "a".repeat(64),
  };

  assert.equal(websiteArtifactIdentity(artifact), `sha256:${"a".repeat(64)}`);
  assert.equal(restoredWebsiteView(artifact, { storage }), "code");
  assert.equal(persistWebsiteView(artifact, "preview", { storage }), "preview");
  assert.equal(restoredWebsiteView({ ...artifact }, { storage }), "preview");
  assert.deepEqual([...values.values()], ["preview"]);
  assert.doesNotMatch([...values.keys()].join("\n"), /Private demo|doctype/);

  const revised = { ...artifact, sha256: "b".repeat(64) };
  assert.notEqual(websiteViewStorageKey(revised), websiteViewStorageKey(artifact));
  assert.equal(restoredWebsiteView(revised, { storage }), "code");

  const linkedRevision = {
    ...revised,
    website_id: "tim-towing",
    parent_sha256: artifact.sha256,
  };
  assert.equal(restoredWebsiteView(linkedRevision, { storage }), "preview");
  persistWebsiteView(linkedRevision, "code", { storage });
  assert.equal(restoredWebsiteView(linkedRevision, { storage }), "code");
});

test("website view persistence fails closed when identity or browser storage is unavailable", () => {
  const brokenStorage = {
    getItem: () => { throw new Error("blocked"); },
    setItem: () => { throw new Error("blocked"); },
  };
  assert.equal(websiteArtifactIdentity({ sha256: "not-a-digest" }), "");
  assert.equal(restoredWebsiteView({}, { storage: brokenStorage }), "code");
  assert.equal(persistWebsiteView({}, "preview", { storage: brokenStorage }), "preview");
  assert.equal(restoredWebsiteView({ sha256: "c".repeat(64) }, { storage: brokenStorage }), "code");
});
