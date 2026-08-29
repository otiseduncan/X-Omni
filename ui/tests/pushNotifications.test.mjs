import assert from "node:assert/strict";
import test from "node:test";

import {
  pushSupported,
  subscriptionPayload,
  urlBase64ToUint8Array,
} from "../src/lib/pushNotifications.js";

test("urlBase64ToUint8Array decodes a VAPID-style unpadded base64url key", () => {
  // "hello" base64url-encoded without padding.
  const decoded = urlBase64ToUint8Array("aGVsbG8");
  assert.deepEqual(Array.from(decoded), [104, 101, 108, 108, 111]);
});

test("urlBase64ToUint8Array handles -/_ substitution", () => {
  const decoded = urlBase64ToUint8Array("--__");
  assert.ok(decoded instanceof Uint8Array);
  assert.equal(decoded.length, 3);
});

test("subscriptionPayload extracts endpoint and keys from a PushSubscription-shaped object", () => {
  const subscription = {
    toJSON: () => ({
      endpoint: "https://push.example.com/abc",
      keys: { p256dh: "p256dh-value", auth: "auth-value" },
    }),
  };
  assert.deepEqual(subscriptionPayload(subscription), {
    endpoint: "https://push.example.com/abc",
    p256dh: "p256dh-value",
    auth: "auth-value",
  });
});

test("subscriptionPayload tolerates missing keys instead of throwing", () => {
  const subscription = { toJSON: () => ({ endpoint: "https://push.example.com/x" }) };
  assert.deepEqual(subscriptionPayload(subscription), {
    endpoint: "https://push.example.com/x",
    p256dh: "",
    auth: "",
  });
});

test("pushSupported is false in this Node test environment (no browser globals)", () => {
  assert.equal(pushSupported(), false);
});
