import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const mainUrl = new URL("../src/main.jsx", import.meta.url);
const cssUrl = new URL("../src/styles/scroll-fix.css", import.meta.url);

test("chat stream remains the vertical scroll owner", async () => {
  const css = await readFile(cssUrl, "utf8");
  assert.match(css, /\.stream\s*\{[\s\S]*?min-height:\s*0\s*!important/);
  assert.match(css, /overflow-y:\s*auto\s*!important/);
  assert.match(css, /touch-action:\s*pan-y/);
  assert.match(css, /-webkit-overflow-scrolling:\s*touch/);
});

test("desktop composer wrapper occupies the composer grid area", async () => {
  const css = await readFile(cssUrl, "utf8");
  assert.match(css, /\.shell\s*>\s*div:has\(>\s*\.composer\)/);
  assert.match(css, /grid-area:\s*composer/);
});

test("scroll hardening loads after the App module styles", async () => {
  const main = await readFile(mainUrl, "utf8");
  const appImport = main.indexOf('import App from "./App.jsx"');
  const scrollImport = main.indexOf('import "./styles/scroll-fix.css"');
  assert.ok(appImport >= 0 && scrollImport > appImport);
});
