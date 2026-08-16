import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  authPanelMode,
  buildGoogleAuthSetupPayload,
  LOCAL_GOOGLE_CALLBACK,
} from "../src/lib/authSetup.js";

test("routes post-restart unowned state to Google login instead of setup", () => {
  assert.equal(
    authPanelMode({
      auth_enabled: true,
      google_configured: true,
      owner_bound: false,
      signed_in: false,
    }),
    "ready"
  );
  assert.equal(authPanelMode({ auth_enabled: false, google_configured: true, signed_in: true }), "setup");
  assert.equal(authPanelMode({ auth_enabled: true, google_configured: true, signed_in: true }), "session");
});

test("builds the exact setup contract and normalises the optional origin", () => {
  assert.deepEqual(
    buildGoogleAuthSetupPayload({
      clientId: " client-id ",
      clientSecret: " client-secret ",
      publicOrigin: " https://omega.example.ts.net/ ",
    }),
    {
      client_id: "client-id",
      client_secret: "client-secret",
      public_origin: "https://omega.example.ts.net",
    }
  );
  assert.equal(LOCAL_GOOGLE_CALLBACK, "http://127.0.0.1:8100/api/auth/callback");
});

test("omits an empty public origin", () => {
  assert.deepEqual(
    buildGoogleAuthSetupPayload({ clientId: "id", clientSecret: "secret", publicOrigin: "" }),
    { client_id: "id", client_secret: "secret" }
  );
});

test("auth UI uses POST boundaries and has no client-side secret persistence or logging", async () => {
  const source = await readFile(new URL("../src/components/AuthPanel.jsx", import.meta.url), "utf8");
  const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/styles/app.css", import.meta.url), "utf8");
  assert.match(source, /fetch\("\/api\/auth\/setup"/);
  assert.match(source, /fetch\("\/api\/auth\/logout"/);
  assert.match(source, /Sign in with Google and become Owner/);
  assert.equal((source.match(/method: "POST"/g) || []).length, 2);
  assert.doesNotMatch(source, /localStorage|sessionStorage|console\.(?:log|debug|info|warn|error)\s*\(/);
  assert.match(app, /aria-label=\{[\s\S]*Open account and sign out[\s\S]*Set up Google Auth/);
  assert.match(styles, /\.icon-btn\s*\{[\s\S]*?width:\s*44px;[\s\S]*?height:\s*44px;/);
  assert.match(styles, /@media \(max-width: 480px\)[\s\S]*?\.brand-text\s*\{/);
});
