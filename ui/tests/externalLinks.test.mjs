import assert from "node:assert/strict";
import test from "node:test";

import { safeExternalUrl } from "../src/lib/externalLinks.js";

test("research links allow ordinary public HTTP(S) sources", () => {
  assert.equal(safeExternalUrl("https://example.com/report?q=one#part"), "https://example.com/report?q=one#part");
  assert.equal(safeExternalUrl("http://93.184.216.34/source"), "http://93.184.216.34/source");
});

test("research links block active, credentialed, local, private, and metadata targets", () => {
  const blocked = [
    "javascript:alert(1)",
    "data:text/html,unsafe",
    "https://user:secret@example.com/",
    "http://localhost/admin",
    "http://127.0.0.1/admin",
    "http://10.0.0.1/",
    "http://100.64.0.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://192.168.1.1/",
    "http://[::1]/",
    "http://[fd00::1]/",
    "http://[::ffff:172.16.0.1]/",
    "http://printer.local/",
    "http://metadata.google.internal/",
  ];

  blocked.forEach((url) => assert.equal(safeExternalUrl(url), null, url));
});
