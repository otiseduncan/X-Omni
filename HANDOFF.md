# X Omni engineering handoff

Updated 2026-08-16 on Omega. This file is the current implementation record; `PLAN.md` retains planning context and is not proof of shipped behavior.

## Scope and invariants

- Workspace: `X:\X Omni` (not currently a Git repository).
- `X:\X 11` and `X:\XV12` were reference-only and were not modified.
- Fixed hardware: RTX 5060 Ti 16GB plus RTX 5060 8GB. No cloud or hardware-upgrade substitution.
- One 30B worker at a time, 32K context. Omni is default; Coder is a manual swap.
- Auth may be disabled only for local first-time setup. Port 8100 must not be proxied remotely while it is off.
- No action is called successful without terminal execution evidence.

## Current architecture

- Core: FastAPI on `127.0.0.1:8100`.
- Worker: llama.cpp on X Omni's dedicated `127.0.0.1:8131`.
- State: SQLite/WAL under `data`, outside the disposable model process.
- UI: React/Vite PWA served by Core.
- External services: Open-Meteo weather, RainViewer/CARTO radar, DuckDuckGo/Google News research, Google OAuth/Calendar, and optional browser/Google speech. Model inference, camera-frame vision, website generation, and ComfyUI image synthesis remain on Omega.
- Model/runtime/weights remain owned under `X:\XV12`; X Omni reads them by configured absolute path and does not modify the reference tree.

## Hardened behavior now implemented

### Owner authentication

- The header always exposes account state: local setup when auth is disabled or unconfigured, Google login when configured, and session logout after Owner sign-in.
- `POST /api/auth/setup` is a one-time, JSON-only credential bootstrap restricted to the literal loopback Host and matching local Origin. It rejects forwarding headers, closes permanently after Owner binding, atomically preserves unrelated `.env.local` entries, and never returns or logs the client secret.
- After setup and restart, the configured-but-unowned UI routes directly to **Sign in with Google and become Owner**; it cannot resubmit the setup form.
- First Owner binding retains the signed Google ID-token, issuer, audience, expiry, nonce, state, verified-email, and local-bootstrap checks. Logout removes only the current browser session.

### Worker lifecycle

- Exact executable, complete command line, model path, alias, port, 32K context, PID, process start time, and GPU attachment form one ownership/readiness contract.
- Both GPUs must be attached before ready and must cross configured free-VRAM thresholds before the next load.
- A foreign or unverifiable listener is never adopted, terminated, or replaced.
- A cross-process file lock serializes lifecycle changes.
- Inference leases let concurrent reads finish and block swaps/recovery until an entire HTTP attempt exits. Recovery occurs only after the lease is released.
- Failed starts clean up only the exact spawned process.
- `/healthz` returns 200 only for the full live model contract; it returns 503 with separate Core liveness and detailed model issues when degraded.

### Approval and execution truth

- Protected action identity binds conversation, session, user, source message, tool-call ID, tool name, and canonical arguments.
- SQLite `BEGIN IMMEDIATE` plus a pending-to-executing compare-and-set selects one executor.
- Terminal success, failure, denial, and expiry create immutable receipts with action/idempotency identities and result hashes.
- Replays return the persisted receipt and never invoke the handler again.
- WebSocket progression is `approval_status`, terminal `approval_receipt`, then compatibility `approval_resolved`.
- Assistant continuation stores an `execution_receipt` artifact. UI success requires `executed=true` and `success=true` in a terminal receipt.
- Legacy prototype approvals migrate to expired, non-executable evidence.

### Identity and secrets

- Google OIDC validates an RS256 signature, issuer, audience, expiry, issued-at time with five-second skew, nonce, verified email, subject agreement, and browser-bound state.
- First Owner binding must begin from the literal loopback Host. Configured Tailscale Host/protocol matching is exact.
- Session cookies are random but SQLite stores only their SHA-256 hashes. Legacy raw-cookie sessions expire during migration.
- Secret/credential paths, SQLite/WAL/SHM state, and private-key locations are invariant-denied to model file tools even inside an allowed root.
- Tool results are bounded and redact secret keys, assignments, bearer tokens, JWTs, Google API keys, and private keys before prompts, WebSockets, or audit material.
- Calendar writes use RFC3339 UTC offsets instead of invalid Windows display-timezone names and reject non-forward intervals.

### Continuity, UI, and privacy

- Initial load restores the server's newest conversation; reconnect re-reads it and merges server truth without optimistic duplicates.
- Long conversations return the newest 500 messages in chronological order.
- Pending approvals reconcile from the backend and coalesce request plus terminal receipt into one card.
- Stored non-sensitive artifacts are compacted, redacted, and rehydrated into later model turns, so grounded follow-ups survive reloads and worker swaps. Approval and execution artifacts are deliberately excluded from prompt replay.
- Current web research, bounded file search, and the live capability catalog persist as ordinary chat artifacts. No new modal, panel, rail, or nested vertical viewport was added.
- The stream follows new tokens only while the reader is near the live edge. Scrolling up is no longer overridden; sending a new message explicitly resumes following.
- Mobile checks cover 360, 390, and 430px. Top controls are at least 44px, the composer remains visible, zoom is enabled, and interactive controls have labels.
- The UI discloses browser/Google speech and RainViewer/CARTO network boundaries. Camera requests render an explicit live preview with Start, Analyze current frame, and Stop controls. No recurring upload exists; raw frames are not persisted, and every track is stopped on Stop or unmount.
- Website generation returns one bounded HTML artifact with an inline Code/Preview flip, Copy code, Download HTML, a network-blocking CSP, and a script-disabled sandboxed preview. It does not silently replace an explicit request to write files or deploy; those still use the approval-gated write/PowerShell path.
- Image generation is a separate approval-gated ComfyUI workload under an exclusive model lifecycle lease. Only a verified, content-addressed PNG with a matching success receipt and proved Omni restoration renders as success.
- The service worker caches only versioned shell/static assets. API, auth, WebSocket, and third-party traffic are network-only.

### Calibration IQ query truth

- Count questions use one `calibration_iq_summary` result with no vehicle rows; list questions use one `calibration_iq_read` result with a bounded visible subset and the verified full count.
- Active work excludes only the exact normalized terminal statuses `Calibration Complete` and `No Calibration Required`. Similar strings are not treated as terminal.
- Completed, no-calibration-required, generic finished/closed, and all-work requests retain explicit terminal/all scope without weakening the active default.
- Collection paging follows the upstream total even when the server returns 20-row pages, advances by raw rows, de-duplicates stable repair-order identities, and refuses to present a capped or early-ended partial count as verified.
- `Show me those` inherits only allow-listed filters from the latest successful Calibration IQ artifact in the same durable conversation. Explicit filters override inherited filters, and each request persists exactly one card before socket delivery.
- Summary totals and breakdown chips are compact, terminal rows are not styled active, numeric phases display as `Phase N`, incomplete collections remain visibly unverified, and 360/390/430px wrapping contracts are tested.

## Reproducible setup and checks

```powershell
cd "X:\X Omni"
.\scripts\setup.ps1
.\.venv\Scripts\python.exe -m pytest -q
npm --prefix ui test
npm --prefix ui run build
.\scripts\start.ps1
```

`setup.ps1` creates/reuses `.venv`, installs `requirements.lock.txt`, uses `npm ci`, preserves any existing `.env.local`, and builds the UI. `start.ps1` refuses to fall back to global Python.

## Evidence boundary

Confirmed locally:

- `.venv` backend/security/lifecycle/capability suite: 186 passed. Python compilation and dependency consistency passed. Ruff is not installed, so lint was unavailable.
- Frontend suite: 47 passed. Vite 8.2.1 production build passed.
- `setup.ps1` completed using the lockfiles and preserved `.env.local`.
- Browser reload restored the same persisted timeline with no console warnings/errors. 360/390/430px checks had no horizontal overflow, retained the composer, and kept top targets at least 44px. The versioned service worker was served and registered.
- The old 8100/8121 runtime was stopped only after exact process/live-model verification. An integrity-checked pre-migration backup is at `data\backups\x_omni-before-hardening-20260815-190702.sqlite`.
- Hardened Omni cold start on 8131: 18.3s. `/healthz`: HTTP 200 with exact identity/start time, alias, 32768 context, and GPU0+GPU1 proof. WebSocket reply: `LIVE_OK_8131`.
- Live protected write: one tool-call row, one immutable receipt, `executed=true`, `success=true`; duplicate approval returned the same receipt with `replayed=true`. Browser reload rendered the persisted Succeeded card.
- Real Omni→Coder swap: 18.2s, correct Coder alias/32K/both-GPU health, streamed `CODER_SWAP_OK`.
- Real Coder→Omni return: 21.5s, correct Omni alias/32K/both-GPU health, streamed `OMNI_RETURN_OK`.
- A second `start.ps1` launch reused the exact existing Core and model PID/start time. Nothing restarted.
- Capability-migration runtime snapshot: Core pid 2184 on 8100; Omni pid 11948 on 8131. `/healthz` returned 200 with exact alias, 32768 context, process/start-time identity, and GPU0+GPU1. PIDs are observational and will change on a later restart.
- Auth is enabled/configured, the Owner is bound, and the live browser session is signed in. Google Calendar read returned HTTP 200 after restart.
- Live chat called the new current-web tool, reached DuckDuckGo and Google News successfully, produced a cited `web_research` card, and restored exactly one copy after reload. The 1280×720 stream had `overflow-y:auto`, 1240px scroll content in a 580px client area, and a visible composer.
- New external boundaries reject recognized secret-bearing queries before egress, disable search-provider redirects, require allowed content types, and cap provider responses at 2 MiB. File search caps directories/entries/files/matches and revalidates every candidate against the read roots.
- PowerShell timeout/nonzero exit now records terminal failure (`executed=true`, `success=false`). An interrupted persisted `executing` claim reopens as terminal indeterminate failure and is never rerun automatically.
- The signed-in browser exercised the physical-camera path end to end: Start displayed the live webcam inline, Analyze current frame produced one persisted description-only Omni observation, Stop cleared the media source, and reload restored exactly one observation with the camera off.
- A real approved ComfyUI generation produced a verified 1024×1024 PNG whose SHA-256 matched its content-addressed filename and receipt. The exact spawned ComfyUI runtime stopped, port 8188 was released, both GPUs were proved free, the exact Omni worker was restored healthy on 8131, and reload restored one receipt-matched image card.
- Live browser delivery used service-worker cache v6 and the rebuilt assets. The Tim's Towing card defaulted to bounded Code, Preview rendered its real `srcDoc` page in an empty sandbox, the same toggle returned to Code, Copy completed, Download reported a request, and the restored baseball PNG occupied a visible 776px card instead of being flex-clipped to 28px.
- Calibration IQ's guarded Production launcher restored its stopped PostgreSQL process without changing source or reseeding records. Fresh authoritative API paging reported Macon as 59 total records: 50 active and 9 `Calibration Complete`; Macon phase 5 reported 15 active: 9 New Arrival, 5 Waiting On Prerequisites, and 1 Repair In Progress, with zero duplicates.
- A fresh signed-in X Omni conversation ran `How many cars are active in Macon?`, `How many are in Macon phase 5?`, and `Show me those.` The stored calls were summary `{shop: Macon}`, summary `{shop: Macon, phase: 5}`, and read `{shop: Macon, phase: 5}`. Text/card totals were 50, 15, and all 15 rows respectively; exactly one card persisted per prompt and reload restored two summary cards plus one list card.

The four live-smoke conversations were removed from the active database after proof so the UI again restores the user's prior conversation. The complete post-proof database is recoverable at `data\backups\x_omni-after-live-proof-20260815-191640.sqlite`; the generated write artifact was moved beside it as `live-approval-smoke-20260815-191145.txt`.

Not yet proved and must stay labeled as such:

- Live receipt-backed Calendar write.
- Phone behavior over the intended Tailscale route.
- Local Omni audio transcription through this application path.

One non-fatal Windows `WinError 10054` Proactor callback was logged during expected Coder load polling; the swap completed healthy and it did not recur on the Omni return. Treat it as diagnostic noise unless it repeats outside lifecycle turnover.

Observed Tailscale state during this work proxied `/` to port 8084 and `:8443` to port 8120, not X Omni port 8100. Re-check before relying on it because external routing state can drift.
