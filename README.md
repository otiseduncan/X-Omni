# X Omni

Local-model operator assistant running on Omega. It has a chat-native UI, current-web research, weather and calendar cards, voice in/out, native Omni camera-frame vision, buffered website generation, approval-gated local image synthesis, and deliberate switching between a multimodal default model and a coding specialist.

Model inference and local tools run on Omega. External paths are explicit in the UI: weather uses Open-Meteo, radar uses RainViewer/CARTO, current-web research uses DuckDuckGo and Google News, Google OAuth and Calendar use Google, and the optional browser speech recognizer may send audio to Google.

---

## What it is

**One model at a time, by necessity.** A single 30B-A3B model at 32K context with no offload consumes ~91% of Omega's combined 24GB VRAM across both GPUs (measured: 14680/16311 MiB on GPU0, 7601/8151 MiB on GPU1). Two models cannot coexist. Switching takes 15–20 seconds, measured in both directions.

| Worker | Role | Vision | Audio |
|---|---|---|---|
| **Omni** (default) | Conversation, planning, operator work, troubleshooting, light coding | yes | yes |
| **Coder** (swap-in) | Substantial implementation, hard debugging, refactors | no | no |

Omni is resident by default because Coder has *zero* multimodal fallback — a hard wall — while Omni has reduced-but-real coding ability, a soft degradation. Switch with `/coder` and `/omni` in chat, or the pill in the top bar.

**Core is persistent, workers are disposable.** Conversation history, task state, approvals, and the audit log live in SQLite outside the model process. That's what makes a mid-task swap survivable — nothing important lives in the thing being killed.

---

## Setup

### 1. Install

```powershell
cd "X:\X Omni"
.\scripts\setup.ps1
```

Creates an isolated `.venv`, installs the validated Python lock set, installs the locked Node packages, creates `config\.env.local` with a generated session secret when missing, verifies model files, runs with project-local Python, and builds the UI. Existing `.env.local` content is never overwritten.

### 2. Run

```powershell
.\scripts\start.ps1
```

Core starts Omni itself (~15–20s cold), then serves on <http://127.0.0.1:8100>.

For first-time setup you can set `XOMNI_AUTH_ENABLED=0` in `config\.env.local`. This is intentionally local-only: port 8100 must not be proxied by Tailscale while auth is disabled. Open the key-shaped account control in the header to enter the Google OAuth client ID and secret without putting either value in browser storage. Calendar remains disconnected until Google is configured.

---

## Remote access from your phone

### 3. Tailscale

```powershell
.\scripts\tailscale-serve.ps1
```

This uses `tailscale serve`, which is **tailnet-private**. **Tailscale Funnel is not used and must remain disabled.** Funnel would publish the operator service to the open internet. On this shared node, Calibration IQ keeps the normal HTTPS endpoint on port 443 and X Omni uses HTTPS port 8443, so each application has an independent browser origin.

HTTPS is not cosmetic here. Browser microphone access requires a secure context, so **voice input silently fails over plain HTTP**. Tailscale's automatic cert is what makes voice work on the phone.

The script prints your tailnet origin. Put it in `config\.env.local`:

```
XOMNI_PUBLIC_ORIGIN=https://omega.your-tailnet.ts.net:8443
```

Before proxying port 8100, configure Google and turn auth on. Core remains bound to loopback. On the exact configured Serve origin, X Omni accepts the `Tailscale-User-Login` identity header that Serve adds after stripping client-supplied copies. The same header on the direct local origin is ignored, and protected remote routes fail closed when it is missing. Then open the HTTPS URL on your phone and use "Add to Home Screen". The service worker caches only the versioned app shell/static assets; API, auth, WebSocket, and third-party traffic are network-only.

### 4. Google sign-in and Calendar

The Owner OAuth consent proves the immutable Owner identity and carries Calendar scopes. Invited testers use the same Google OIDC implementation with identity-only `openid email profile` scopes; tester tokens never replace the Owner's Calendar connection.

In [Google Cloud Console](https://console.cloud.google.com/apis/credentials):

1. Create an **OAuth 2.0 Client ID**, type **Web application**
2. Add both authorized redirect URIs:
   - `http://127.0.0.1:8100/api/auth/callback`
   - `https://omega.your-tailnet.ts.net:8443/api/auth/callback`
3. Enable the **Google Calendar API** for the project
4. At the literal local URL, open the header account control and save the client ID and secret. X Omni atomically updates `config\.env.local`, enables auth, and tells you when Core must be restarted. The same values can still be entered in the file manually.
5. Restart Core, then choose **Sign in with Google and become Owner** locally. Once signed in, the header account control provides **Sign out** for the current browser session.

**Sole-owner binding:** the first successful sign-in must begin at the literal local loopback URL and permanently binds the instance to that verified Google identity. Signed ID token, issuer, audience, expiry, nonce, browser-bound state, subject, and verified-email claims are checked. Other accounts are refused. There is no in-app rebind; changing owners requires an intentional, backed-up state reset and loses local state.

### 5. Remote tester onboarding

Tailscale, Google, and X Omni have separate responsibilities:

```text
Tailscale = private network identity and access gate
Google = verified X Omni user identity
X Omni profile = application authorization and per-user data boundary
```

1. Sign in locally as Owner and open the key-shaped **Account** panel.
2. Under **Authorized test users**, enter the tester's exact email. Optionally paste the Tailscale invitation URL created in the Tailscale admin console.
3. X Omni stores the pending authorization and locally generates a QR image for that Tailscale URL. The URL is not sent to a QR website and is not written to audit details.
4. Have the tester scan/save the QR, accept the Tailscale invitation, and authenticate their device to the tailnet.
5. The tester opens the private X Omni Serve URL. Serve supplies the authenticated Tailscale login.
6. The tester chooses Google sign-in with the **same normalized email**. A different or unverified Google email is rejected.
7. On the first successful callback, X Omni stores the durable Google `sub`, verified email/profile, and Tailscale login on the pending user, marks it active, and creates a user-bound session. Later logins reuse that profile.

Test users receive the `test_user` role. Their conversations, messages, tasks, weather preferences, approvals, generated-media reads, and sessions are scoped by application user ID. They may chat and use the small tester tool allowlist, but cannot use Owner administration, Calendar, ADAS, exterior camera, filesystem, PowerShell, model switching, or media-generation capabilities.

Revoking a tester in X Omni immediately invalidates their X sessions while preserving historical profile/data. Removing the tester or device from the tailnet is a separate Tailscale control. **Require re-enrollment** is the explicit workflow that clears the old Google/Tailscale link before another binding; identities are never silently rebound.

---

## Using it

Every model-invoked capability renders in the conversation. New capability
results do not open a dashboard, workspace, modal, or pop-up; the existing
chat stream owns vertical scrolling.

- *"what's the weather"* → weather card inline
- *"what's on my calendar this week"* → agenda card inline
- *"search the live web for…"* → source-linked research card inline
- *"generate a website for Tim's Tow Truck"* → an inline Code card with a Preview/Code flip, Copy code, and Download HTML
- *"turn on the camera and tell me what you see"* → explicit live preview, Analyze current frame, and Stop controls inline
- *"create an image of a baseball"* → approval card, sequential local ComfyUI generation, then a receipt-backed image inline
- *"find this text under X:\\X 11"* → bounded file-search results inline
- *"what can you do right now?"* → truthful live capability catalog inline
- *"/coder refactor this function"* → switches worker (visible countdown), then answers
- Mic button → choose local Omni transcription or the explicitly disclosed browser/Google engine
- Speaker button → replies are read aloud, avatar shows the speaking state

**The avatar is status, not decoration.** Idle, listening, thinking, speaking, and switching each look distinct. The switching state exists specifically because a silent 15–20 second pause is indistinguishable from a crash.

**Local audio and camera-frame vision require Omni.** Coder cannot process either and there is no room for a second resident model. The browser camera stays visibly live only after the operator starts it; Omni receives one bounded current frame only when **Analyze current frame** is chosen. Raw frames are neither stored nor placed in chat state. Arbitrary image attachment is still not wired.

**Image synthesis is a separate worker, not an Omni output mode.** `image_generate` requires approval, waits for active inference, stops only the exactly verified Omni process, starts an X Omni-owned ComfyUI runtime, validates and content-addresses the PNG, stops that exact runtime, proves GPU release, and restores the previous model before a success card can appear.

---

## Safety model

Every tool call passes the capability gateway in `config\tools.yaml` before running. The model's output is a *request* to run a tool, never permission to run one.

| Tier | Behavior |
|---|---|
| `read_only` | Runs immediately, logged |
| `confirm_required` | Approval card in chat, waits for you |
| `blocked` | Never runs |

A tool with no entry is **blocked** — fail closed, not open. Path-taking tools are confined to configured roots after canonical resolution. Secret/credential files, SQLite state, WAL/SHM files, and private-key locations remain blocked even if they sit under an allowed root. `write_file`, `run_powershell`, `create_calendar_event`, `add_task`, `update_task_status`, and `image_generate` require explicit approval. Approved PowerShell remains unrestricted command execution; its write authority was not narrowed. `delete_file` is blocked outright. `X:\X 11` and `X:\XV12` remain reference-only roots for the file tool.

Approval identity binds the exact conversation, browser session, user, source message, tool call, tool name, and arguments. A SQLite compare-and-set allows one executor only. Terminal success, failure, denial, and expiry produce immutable receipts; a replay returns the same receipt without re-running the action. The UI never labels an action successful unless its receipt says `executed=true` and `success=true`.

The audit log is append-only. Session cookies are stored only as SHA-256 hashes. Secret paths are unavailable to model file tools, and tool results are bounded and redacted before reaching prompts, WebSockets, or logs. Search queries containing recognized credentials are rejected before external egress; provider bodies and filesystem traversal are bounded. `config\.env.local` is gitignored.

---

## Layout

```
core\
  main.py              entrypoint, wiring
  config.py            settings from env
  models\
    router.py          the swap primitive
    client.py          OpenAI-compatible streaming
  orchestrator\
    loop.py            turn loop, tool calls, artifacts
    prompt.py          system prompt, context budget
  api\
    auth.py            Google OIDC, Tailscale Serve identity, Owner/tester sessions
    chat.py            chat WebSocket
    routes.py          REST + voice transcription
  services\            weather, calendar, research, Google auth, camera vision,
                       website preview, sequential image generation, bounded
                       procedural video, sequential Wan image-to-video
  state\               SQLite schema and access
  tools\               capability gateway, builtin tools
ui\                    React + Vite, mobile-first PWA
config\
  workers.json         model paths, ports, capabilities
  tools.yaml           capability policy and allowed roots
  .env.local           secrets (gitignored)
data\                  SQLite lives here
```

Model weights and the llama.cpp runtime stay where they are under `X:\XV12\...`. X Omni reads them by absolute path and never writes into that tree. X11 and XV12 source trees are also reference-only file-search roots. To move model assets, edit `config\workers.json`.

---

## Troubleshooting

**"Could not start default worker"** — check the paths in `config\workers.json`. `setup.ps1` verifies them and reports which are missing.

**Swap fails, or VRAM never frees** — X Omni's dedicated worker port is 8131. Core verifies the listener's executable, full command line, PID/start time, model alias, 32K context, and both-GPU attachment before it adopts or stops anything. A foreign or unverifiable listener is reported and left untouched. Use the reported PID/issue to resolve the conflict; elevate only if Windows explicitly denies process inspection.

**VRAM won't release below the threshold** — a hung process or orphaned CUDA context is still holding it. Check `nvidia-smi`, then stop the offending process by PID.

**Microphone never prompts** — you're on plain HTTP. Use the Tailscale HTTPS address, not `http://<lan-ip>:8100`.

**Google sign-in returns `no_refresh_token`** — Google only issues a refresh token on first consent. Revoke X Omni's access at <https://myaccount.google.com/permissions> and sign in again.

**`not_owner`** — you signed in with a different Google account than the one bound on first login. That's the sole-owner guard working.

---

## What isn't built

Arbitrary URL fetching/full-page extraction, arbitrary image attachments, finance quotes, autonomous continuous camera interpretation, image-to-image editing, reusable 3D-mesh reconstruction, and automatic model routing are not built. The PC webcam and configured exterior camera have operator-controlled live in-chat previews, but Omni analyzes only explicitly submitted current frames. Video creation has two explicit non-interchangeable modes: the proved deterministic `exact_source_animation` treatment and an `image_to_video` Wan2.2 TI2V-5B diffusion path. The Wan path is source-conditioned 2D video with depth-like motion, not a reusable 3D object; its three installed official model files pass pinned size and SHA-256 proof. A live 10-second, 240-frame Wan run completed on Omega with authenticated in-chat playback and verified Omni restoration. Frame analysis proved localized generative orb motion against a stable background rather than a whole-frame wobble, while also showing the current quality limitation: surface shimmer/morphing can replace fine source details and is not the same as a clean rigid 3D rotation. A Wan failure never falls back to procedural motion. Current web search is deliberately source-snippet bounded. Routing remains manual because a model swap is a real, visible operation.

---

## Verified vs. not

Validated on Omega in the project `.venv`: 261 backend/security/lifecycle/capability tests and 56 frontend tests pass; Python compilation and the Vite 8.2.1 production build pass; and `pip check` is clean. Tests cover remote tester provisioning/reuse, Tailscale/Google mismatch and conflict rejection, revoked-session invalidation, owner/tester data and tool isolation, local-only QR generation, the live camera stream lifecycle and cleanup, raw bounded frame transport, website integrity/sandboxing, website copy/download controls, generated-image layout/error handling, image-runtime ownership/cancellation/model restoration, digest-only video sources, bounded FFmpeg cleanup, the fixed 241-to-240-frame Wan graph, official model size/SHA proof, Wan cancellation/release/restore partial truth, bounded pre-stop readiness retries with exact ownership re-proof, single-shot indeterminate prompt submission, durable model-free failure reporting, strict MP4/receipt proof, authenticated Range playback, receipt pairing, reload continuity, and Calibration IQ summary/list pagination, terminal filtering, deduplication, contextual follow-ups, and compact field-card rendering. Browser checks at 360, 390, and 430px showed no horizontal overflow, retained the composer, and kept top controls at least 44px; the new Owner tester-enrollment panel also fit at 390×844 with no horizontal overflow.

The lifecycle test suite proves foreign-listener refusal, exact process/start-time ownership, live alias plus 32K context checks, two-GPU readiness/release, cross-process locking, failure cleanup, and swap exclusion during inference. `/healthz` is 200 only with the complete live model contract; degraded Core liveness is reported separately with HTTP 503.

Real-hardware proof also passed: hardened Omni cold-started on dedicated port 8131 in 18.3s and streamed an exact reply; Omni→Coder swapped in 18.2s and streamed from the Coder alias; Coder→Omni returned in 21.5s and streamed again. Every ready state proved exact process/start time, alias, 32K context, and both GPUs. A live protected write produced one tool-call row and one immutable successful receipt; submitting the same approval again returned that receipt with `replayed=true` and did not execute again. Browser reload rendered the persisted receipt-backed Succeeded card. The capability migration restarted only verified Core, adopted the same verified Omni worker, and live chat invoked DuckDuckGo plus Google News before returning and persisting a cited research card.

The live camera path was also exercised in the signed-in browser: Start displayed the physical webcam stream inline, Analyze current frame sent one bounded frame to Omni and persisted one description-only observation, and Stop cleared the media source. Reload restored exactly one observation and left the camera off. A real approved ComfyUI run generated and hash-verified a 1024×1024 PNG, stopped the exact spawned image runtime, proved both GPUs released, restored the exact Omni worker, and rendered one receipt-matched image after reload. A later approved `video_generate` call animated that exact orb image into a verified 10-second, 1024×1024, 24 fps H.264/yuv420p MP4 with 240 frames. A second source-conditioned Wan run produced a separately verified 704×704, 240-frame, 10-second MP4 after a prior request stopped at the pre-handoff readiness proof; ComfyUI shut down, Omni restored, and reload retained exactly one success player while clearing the stale workload state. The signed-in browser decoded the authenticated Range stream and retained its download link. The signed-in browser also flipped the Tim's Towing artifact from bounded Code to a real sandboxed iframe and back, copied the exact HTML, dispatched the HTML download, and visibly rendered the restored baseball image at its full reserved card height.

Still requiring external/live proof: the complete spare-account Tailscale/Google tester enrollment and revocation acceptance test, Calendar write, and local Omni audio transcription. The live Serve route, migrated runtime, signed-in Owner UI, Google OAuth/Owner binding, and Calendar read are proved; those do not substitute for testing a second real Tailscale and Google identity on a tester device.
