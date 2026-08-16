import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("exterior camera setup and live controls stay explicit and inside the chat artifact", async () => {
  const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
  const cards = await readFile(new URL("../src/components/cards/Cards.jsx", import.meta.url), "utf8");
  const capture = await readFile(new URL("../src/lib/cameraCapture.js", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/styles/app.css", import.meta.url), "utf8");
  const exteriorBlock = cards.slice(
    cards.indexOf("function ExteriorCameraRequestCard"),
    cards.indexOf("function CameraObservationCard")
  );

  assert.match(cards, /exterior_camera_request:\s*ExteriorCameraRequestCard/);
  assert.match(cards, /label:\s*"Exterior camera"/);
  assert.match(cards, /host:\s*"192\.168\.1\.10"/);
  assert.match(cards, /username:\s*"admin"/);
  assert.match(cards, /<form[\s\S]*?aria-label="Exterior camera setup"/);
  assert.match(cards, /htmlFor=\{passwordId\}[\s\S]*?type="password"[\s\S]*?autoComplete="off"[\s\S]*?data-1p-ignore="true"/);
  assert.match(cards, /aria-label="Save exterior camera setup"/);
  assert.match(cards, /onClick=\{startExteriorCamera\}[\s\S]*?Start live feed/);
  assert.match(cards, /<img[\s\S]*?src=\{session\.stream_url\}[\s\S]*?alt=\{`\$\{label\} live feed`\}/);
  assert.match(exteriorBlock, /\{frameReady && !streamFailed && \([\s\S]*?>Live<\/span>/);
  assert.match(cards, /onClick=\{analyzeExteriorFrame\}[\s\S]*?Analyze current frame/);
  assert.match(exteriorBlock, /disabled=\{busy \|\| streamFailed \|\| !frameReady\}/);
  assert.match(exteriorBlock, /cameraSourceId: "exterior",[\s\S]*?cameraSessionId: sessionRef\.current\?\.session_id/);
  assert.doesNotMatch(exteriorBlock, /\{ image: imageRef\.current/);
  assert.match(cards, /onClick=\{disconnectExteriorCamera\}[\s\S]*?Disconnect \/ log out/);
  assert.match(cards, /role="group" aria-label="Exterior camera controls"/);
  assert.match(cards, /role="status"/);
  assert.match(cards, /role="alert"/);
  assert.match(cards, /stopCallbackRef\.current\(activeSession\.session_id, \{ keepalive: true \}\)/);
  assert.match(cards, /imageRef\.current\?\.removeAttribute\?\.\("src"\)/);
  assert.doesNotMatch(exteriorBlock, /role="dialog"|aria-modal|window\.open|target="_blank"/);

  const firstPasswordClear = cards.indexOf('passwordRef.current.value = ""');
  const configureCall = cards.indexOf("const pending = onExteriorCameraConfigure");
  assert.ok(firstPasswordClear >= 0 && firstPasswordClear < configureCall,
    "the password field must clear before the configuration request is awaited");
  assert.doesNotMatch(exteriorBlock, /localStorage|sessionStorage|indexedDB|data:image\/jpeg;base64/);
  assert.doesNotMatch(`${exteriorBlock}\n${capture}`, /setInterval|requestAnimationFrame/);

  assert.match(styles, /\.exterior-camera-field input\s*\{[\s\S]*?min-height:\s*44px/);
  assert.match(styles, /\.camera-action\s*\{[\s\S]*?min-height:\s*44px/);
  assert.match(styles, /@media \(max-width: 480px\)[\s\S]*?\.exterior-camera-fields\s*\{[\s\S]*?grid-template-columns:\s*1fr/);
  assert.match(styles, /\.exterior-camera-live-image[\s\S]*?width:\s*100%[\s\S]*?height:\s*100%/);
  assert.match(styles, /\.stream > \*\s*\{[\s\S]*?flex-shrink:\s*0/);
});

test("exterior camera API and still analysis use the bounded same-origin contracts", async () => {
  const app = await readFile(new URL("../src/App.jsx", import.meta.url), "utf8");
  const capture = await readFile(new URL("../src/lib/cameraCapture.js", import.meta.url), "utf8");
  const sw = await readFile(new URL("../public/sw.js", import.meta.url), "utf8");

  assert.match(app, /fetch\("\/api\/cameras\/exterior",[\s\S]*?method:\s*"GET"/);
  assert.match(app, /fetch\("\/api\/cameras\/exterior\/configure",[\s\S]*?method:\s*"POST"/);
  assert.match(app, /JSON\.stringify\(\{ label, host, username, password \}\)/);
  assert.match(app, /fetch\("\/api\/cameras\/exterior\/sessions",[\s\S]*?JSON\.stringify\(\{ conversation_id: conversationId \}\)/);
  assert.match(app, /`\/api\/cameras\/exterior\/sessions\/\$\{encodeURIComponent\(safeSessionId\)\}`[\s\S]*?method:\s*"DELETE"/);
  assert.match(app, /safeExteriorCameraSession\(payload, window\.location\)/);
  assert.match(app, /headers\["X-XOmni-Camera-Source-ID"\] = "exterior"/);
  assert.match(app, /headers\["X-XOmni-Camera-Session-ID"\] = exteriorSessionId/);
  assert.match(app, /\^\[A-Za-z0-9_-\]\{8,160\}\$\/\.test\(exteriorSessionId\)/);
  assert.match(app, /request\.body = frame\.blob/);
  assert.match(capture, /if \(blob\.size > CAMERA_MAX_JPEG_BYTES\)/);
  assert.match(capture, /resolved\.origin !== origin/);
  assert.doesNotMatch(`${app}\n${capture}`, /captureCameraImageJpeg|waitForImageFrame|drawImage\(image/);
  assert.doesNotMatch(`${app}\n${capture}`, /toDataURL|FileReader|FormData/);

  const captureBlock = app.slice(
    app.indexOf("async function captureAndAnalyzeCamera"),
    app.indexOf("async function newConversation")
  );
  const requestBlock = captureBlock.slice(
    captureBlock.indexOf("const headers ="),
    captureBlock.indexOf('const response = await fetch("/api/vision/analyze"')
  );
  const exteriorArm = requestBlock.slice(
    requestBlock.indexOf("if (isExterior)"),
    requestBlock.indexOf("} else {")
  );
  const browserArm = requestBlock.slice(requestBlock.indexOf("} else {"));
  assert.match(exteriorArm, /X-XOmni-Camera-Source-ID/);
  assert.match(exteriorArm, /X-XOmni-Camera-Session-ID/);
  assert.doesNotMatch(exteriorArm, /Content-Type|request\.body/);
  assert.match(browserArm, /Content-Type/);
  assert.match(browserArm, /request\.body = frame\.blob/);

  const apiBypass = sw.indexOf('url.pathname.startsWith("/api/")');
  const cacheIntercept = sw.indexOf("event.respondWith(staticResponse(request))");
  assert.ok(apiBypass >= 0 && apiBypass < cacheIntercept,
    "MJPEG API streams must bypass the service-worker cache");
});
