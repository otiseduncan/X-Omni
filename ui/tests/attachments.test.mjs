import assert from "node:assert/strict";
import test from "node:test";

import {
  humanSize,
  kindLabel,
  splitAttachmentBlocks,
  tooLarge,
  MAX_ATTACHMENT_BYTES,
} from "../src/lib/attachments.js";

/* Core stores one message containing both what Otis typed and the text X read
   out of each file. The chat bubble must show the message, not the document. */

test("a message with no attachments is returned untouched", () => {
  const { text, blocks } = splitAttachmentBlocks("what is the torque spec?");
  assert.equal(text, "what is the torque spec?");
  assert.deepEqual(blocks, []);
});

test("multi-line plain messages keep every line", () => {
  const body = "line one\nline two\n\nline four";
  assert.equal(splitAttachmentBlocks(body).text, body);
});

test("the typed question is separated from the extracted file text", () => {
  const stored = [
    "what is the target distance?",
    "",
    "[Attachment: spec.pdf -- PDF, 4 page(s), 2.0 KB, id 7]",
    "--- page 1 ---",
    "Target distance 1.5 m",
  ].join("\n");

  const { text, blocks } = splitAttachmentBlocks(stored);
  assert.equal(text, "what is the target distance?");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].id, 7);
  assert.equal(blocks[0].filename, "spec.pdf");
  assert.equal(blocks[0].descriptor, "PDF, 4 page(s), 2.0 KB");
  assert.match(blocks[0].body, /Target distance 1\.5 m/);
});

test("several attachments each become their own block", () => {
  const stored = [
    "look at both of these",
    "",
    "[Attachment: a.txt -- text file, 12 B, id 1]",
    "alpha contents",
    "",
    "[Attachment: b.xlsx -- Excel workbook, 3.0 KB, id 2]",
    "--- sheet: ROs ---",
    "12345 | open",
  ].join("\n");

  const { text, blocks } = splitAttachmentBlocks(stored);
  assert.equal(text, "look at both of these");
  assert.deepEqual(blocks.map((block) => block.id), [1, 2]);
  assert.match(blocks[0].body, /alpha contents/);
  assert.match(blocks[1].body, /12345 \| open/);
  // The first block must not swallow the second.
  assert.doesNotMatch(blocks[0].body, /sheet: ROs/);
});

test("files sent with no typed message show an empty bubble, not the preamble", () => {
  const stored = [
    "(The operator attached the following with no message text.)",
    "",
    "[Attachment: label.jpg -- image, 240.0 KB, id 9]",
    "X's reading of this image:",
    "Part number 89340-12345",
  ].join("\n");

  const { text, blocks } = splitAttachmentBlocks(stored);
  assert.equal(text, "");
  assert.equal(blocks.length, 1);
  assert.match(blocks[0].body, /89340-12345/);
});

test("an attachment-looking line inside ordinary prose does not split the message", () => {
  // No trailing ", id N]", so it is not a block header.
  const body = "I wrote [Attachment: something] in my notes yesterday";
  const { text, blocks } = splitAttachmentBlocks(body);
  assert.equal(text, body);
  assert.deepEqual(blocks, []);
});

test("a block header only counts at the start of a line", () => {
  const body = "see this [Attachment: x.pdf -- PDF, 1.0 KB, id 3] inline";
  const { text, blocks } = splitAttachmentBlocks(body);
  assert.equal(text, body);
  assert.deepEqual(blocks, []);
});

test("empty and nullish messages are handled", () => {
  assert.deepEqual(splitAttachmentBlocks(""), { text: "", blocks: [] });
  assert.deepEqual(splitAttachmentBlocks(null), { text: "", blocks: [] });
  assert.deepEqual(splitAttachmentBlocks(undefined), { text: "", blocks: [] });
});

test("size formatting crosses units readably", () => {
  assert.equal(humanSize(0), "0 B");
  assert.equal(humanSize(900), "900 B");
  assert.equal(humanSize(2048), "2.0 KB");
  assert.equal(humanSize(5 * 1024 * 1024), "5.0 MB");
});

test("kind labels name the file type a person would recognise", () => {
  assert.equal(kindLabel("xlsx"), "Excel workbook");
  assert.equal(kindLabel("docx"), "Word document");
  assert.equal(kindLabel("pdf"), "PDF");
  assert.equal(kindLabel("nonsense"), "file");
});

test("oversized files are caught before they leave the browser", () => {
  assert.equal(tooLarge({ size: MAX_ATTACHMENT_BYTES }), false);
  assert.equal(tooLarge({ size: MAX_ATTACHMENT_BYTES + 1 }), true);
  assert.equal(tooLarge({}), false);
});
