import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");

function loadNearBottomHelper() {
  const match = source.match(
    /export function isNearChatBottom\(element, threshold = 72\) \{[\s\S]*?\n\}/
  );
  assert.ok(match, "App must expose the near-bottom calculation for deterministic verification");
  const body = match[0].replace("export ", "");
  return Function(`${body}\nreturn isNearChatBottom;`)();
}

test("near-bottom detection preserves reader intent with a small follow threshold", () => {
  const isNearChatBottom = loadNearBottomHelper();
  assert.equal(isNearChatBottom(null), true);
  assert.equal(
    isNearChatBottom({ scrollHeight: 1000, scrollTop: 500, clientHeight: 440 }),
    true,
    "60px from the end should continue following"
  );
  assert.equal(
    isNearChatBottom({ scrollHeight: 1000, scrollTop: 487, clientHeight: 440 }),
    false,
    "73px from the end should preserve the reader's position"
  );
});

test("stream updates follow only near the bottom and sending returns to the live edge", () => {
  assert.match(source, /const followStreamRef = useRef\(true\);/);
  assert.match(
    source,
    /if \(el && followStreamRef\.current\) el\.scrollTop = el\.scrollHeight;/
  );
  assert.doesNotMatch(source, /if \(el\) el\.scrollTop = el\.scrollHeight;/);
  assert.match(
    source,
    /onScroll=\{\(event\) => \{\s*followStreamRef\.current = isNearChatBottom\(event\.currentTarget\);/
  );

  const resume = source.indexOf("followStreamRef.current = true;");
  const optimisticMessage = source.indexOf('push({ kind: "user", text: body });', resume);
  assert.ok(resume >= 0 && optimisticMessage > resume, "a successful send must resume following before render");
});
