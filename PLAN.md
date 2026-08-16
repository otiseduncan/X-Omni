# X Omni — Build Plan

**Status:** architecture locked, ready to build
**Location:** `X:\X Omni` (empty, clean slate)
**Supersedes:** nothing. This is a from-scratch build.

---

## 0. What this is, and what it is not

X Omni is a **new, standalone local AI operator assistant** built from zero at `X:\X Omni`.

It is **not** a fork of XV11. It is **not** a patch to XV12. Neither of those codebases is modified, imported, or depended on at runtime. They are reference material only:

- **XV11** informs the *visual language* — deep navy/black field, blue-cyan glow borders, the avatar presence treatment, chat-native card rendering.
- **XV12** informs the *orchestration approach* — a single clean model seam, a capability gateway that authorizes tool calls independently of the model, artifacts rendered inline in the conversation.

The one artifact carried forward from this session's work is the **model swap primitive**, because it was measured against Omega's actual hardware rather than designed on paper. Everything else is written new.

---

## 1. Hardware reality (measured on Omega, not assumed)

**Omega:**
- CPU: i7-12700KF, 12C/20T, no iGPU (KF part)
- RAM: 32GB DDR4-3200
- GPU0 / CUDA0: RTX 5060 Ti 16GB, PCIe4 x8, headless
- GPU1 / CUDA1: RTX 5060 8GB, PCIe3 x1, drives the 3440×1440 display
- Windows-native llama.cpp. No WSL. No Docker.

**The measurement that shapes the architecture.** With production Coder running at 32K:

| GPU | Used | Total | Free |
|---|---|---|---|
| GPU0 (16GB) | 14680 MiB | 16311 MiB | 1371 MiB |
| GPU1 (8GB) | 7601 MiB | 8151 MiB | 295 MiB |

A single 30B-A3B model at 32K with no offload spans **both** GPUs and consumes ~91% of the combined 24GB pool. The two leftover pockets sit on separate physical cards and cannot be combined. Neither can host anything — not a small vision model, not a Whisper model, nothing.

**Conclusion: strict single-tenancy. Exactly one model worker resident at any moment.** This is a hardware fact, not a design preference. Any feature that assumes two models running concurrently is impossible here.

**Swap cost, measured directly** (stop by port → confirm VRAM released via `nvidia-smi` → cold start → poll health):

| Transition | Time |
|---|---|
| VRAM release after kill | ~1.1–1.2s |
| Coder → Omni | 20.5s |
| Omni → Coder | 16.3s |

Consistent in both directions regardless of file-cache state — X: is fast enough that cold vs. warm barely registers. **Treat ~15–20s as the real, repeatable cost of every model swap.** Tolerable and predictable, but not free, and the UI must never let it look like a hang.

---

## 2. Hard constraints (do not relitigate without new evidence)

- **32K context is mandatory.** Never lower it as a fix.
- **No CPU/RAM transformer-layer offload.** Sole exception: Omni's mmproj projector in system RAM via `--no-mmproj-offload` — that's projector memory, not model layers, and it's proven working.
- **Q8 KV cache for Coder: rejected.** Saved ~1.55GB VRAM but produced a wrong arithmetic result (prime sum 201 vs. correct 251). Correctness regression is disqualifying.
- **Qwen3-VL 30B: rejected.** OOM at 32K with projector. Deleted.
- **SmolVLM-500M: rejected as production vision.** OCR fine, spatial/color/object reasoning too weak.
- **PowerShell:** complete self-contained `& { ... }` blocks, foreground output, no dangling fragments, no silent backgrounding.
- **Process control by port/process ownership only.** Never generic Python process killing — Calibration IQ runs Python and must not be touched.
- **Do not** move the display to motherboard video (KF CPU, no iGPU) or swap physical GPU positions (worsens topology).

---

## 3. Locked decisions

| Decision | Choice | Reasoning |
|---|---|---|
| Base model | **Qwen3-Omni 30B A3B** resident by default | Coder has *zero* multimodal fallback — a hard wall. Omni has reduced-but-real coding ability — soft degradation. Vision/audio needs are incidental interrupts mid-conversation; coding is a deliberate sit-down session. One 15–20s tax per coding session beats repeated taxes on every glance at a screenshot. |
| Specialist | **Qwen3-Coder 30B A3B** swap-in | Measured edge on hard implementation and schema discipline (7/8 vs 6/8). Invoked deliberately, not automatically. |
| Backend | **Python + FastAPI** | Windows-native, no Docker, async-native for streaming, matches the validated swap primitive. |
| Frontend | **React + Vite**, mobile-first, PWA | Grows into multiple surfaces cleanly; installable on phone. |
| Auth | **Google OAuth, sole owner** | One consent flow does double duty: authenticates you *and* carries Calendar scopes. |
| Remote access | **Tailscale `serve`** | Private tailnet only, automatic valid HTTPS cert, nothing exposed publicly, no port forwarding. |
| Calendar | **Real Google Calendar** | Read live, writes gated behind explicit approval. |
| Voice input | **MediaRecorder → Omni native audio** (not Web Speech API) | See §7 — this is a deliberate deviation worth reading. |
| Voice output | Browser `SpeechSynthesis` | Universally supported, zero backend cost. |
| Routing | **Deterministic, manual override in v1** | Same principle as tool authorization: never delegate to model judgment until real usage justifies it. |

---

## 4. Architecture

```
┌─────────────────────────────────────────────────┐
│  Phone / Desktop browser                        │
│  React PWA · mobile-first · chat-native         │
│  avatar · voice · inline cards                  │
└───────────────────┬─────────────────────────────┘
                    │ HTTPS (Tailscale serve)
                    │ WebSocket for chat stream
┌───────────────────▼─────────────────────────────┐
│  X Omni Core  (FastAPI, 127.0.0.1:8100)         │
│                                                 │
│  auth · session · owner binding                 │
│  conversation + task state (SQLite WAL)         │
│  capability gateway (tool authorization)        │
│  audit log (append-only)                        │
│  model lifecycle manager  ◄── the swap primitive│
│  services: weather · calendar · voice           │
└───────────────────┬─────────────────────────────┘
                    │ OpenAI-compatible HTTP
┌───────────────────▼─────────────────────────────┐
│  ONE model worker  (llama-server.exe :8131)     │
│  Omni (default)  ⇄  Coder (swap-in)             │
│  never both — hardware forbids it               │
└─────────────────────────────────────────────────┘
```

**Core is persistent. Workers are disposable.** Core owns every piece of durable state — conversation history, task state, tool results, approvals, the audit trail. A worker holds nothing but KV cache and transient inference state. This is what makes a 15–20s swap survivable mid-task: nothing important lives in the thing being killed.

**Swap sequence** (each step verified, not assumed):
1. Persist conversation and task state to SQLite
2. Find the PID owning the worker port
3. Terminate it (graceful, then force after 10s)
4. Poll `nvidia-smi` until GPU0 free VRAM crosses threshold — *actual* release, not just process exit
5. Start the next worker
6. Poll `/v1/models` until it answers 200
7. Rebuild the model client against the new worker
8. Resume — Core replays context from SQLite

---

## 5. Folder structure

```
X:\X Omni\
├─ core\
│  ├─ main.py                  entrypoint
│  ├─ config.py                settings, .env loading
│  ├─ api\
│  │  ├─ chat.py               WebSocket chat stream
│  │  ├─ auth.py               Google OAuth, session cookies
│  │  ├─ workers.py            swap / status endpoints
│  │  ├─ calendar.py
│  │  ├─ weather.py
│  │  └─ voice.py              audio upload → transcription
│  ├─ models\
│  │  ├─ router.py             ◄── the swap primitive
│  │  ├─ client.py             OpenAI-compatible streaming client
│  │  └─ configs.py            worker definitions
│  ├─ state\
│  │  ├─ schema.sql
│  │  └─ db.py
│  ├─ tools\
│  │  ├─ registry.py           capability gateway
│  │  └─ builtin\              file_read, list_dir, web_search…
│  ├─ services\
│  │  ├─ google_auth.py        OAuth flow + token store
│  │  ├─ calendar.py
│  │  └─ weather.py            Open-Meteo, no API key
│  └─ orchestrator\
│     ├─ loop.py               tool-call loop
│     └─ prompt.py             system prompt assembly
├─ ui\
│  ├─ src\
│  │  ├─ App.jsx
│  │  ├─ components\
│  │  │  ├─ Avatar.jsx         idle / thinking / speaking / swapping
│  │  │  ├─ ChatStream.jsx
│  │  │  ├─ Composer.jsx       text + mic
│  │  │  ├─ VoiceControls.jsx
│  │  │  └─ cards\             WeatherCard, CalendarCard, ToolCard…
│  │  ├─ hooks\
│  │  ├─ styles\
│  │  └─ theme.css             XV11-derived palette
│  ├─ public\
│  │  ├─ avatar\               idle.mp4, speaking.mp4
│  │  ├─ manifest.webmanifest
│  │  └─ icons\
│  └─ vite.config.js
├─ config\
│  ├─ workers.json
│  ├─ tools.yaml
│  └─ .env.local               secrets — gitignored, never committed
├─ scripts\
│  ├─ start-x-omni.ps1         backend + frontend + Tailscale check
│  ├─ launch-omni.ps1          manual worker launch
│  └─ launch-coder.ps1
├─ data\                       SQLite lives here
└─ logs\
```

Model weights and the llama.cpp runtime **stay where they are** under `X:\XV12\models\...` and `X:\XV12\runtime\...`. `config\workers.json` points at those absolute paths. Moving ~40GB of GGUFs and a working CUDA build to satisfy a folder name buys nothing and risks breaking a known-good runtime. X Omni reads those files; it does not modify anything in that tree.

---

## 6. Data model (SQLite, WAL)

```sql
owner            -- exactly one row: google_sub, email, created_at
sessions         -- id, owner_sub, expires_at, user_agent
conversations    -- id, title, started_at, updated_at
messages         -- id, conversation_id, role, content,
                 --   worker_used, artifacts_json, created_at
tool_calls       -- id, message_id, tool_name, args_json,
                 --   result_json, approved_by, created_at
tasks            -- id, conversation_id, status, plan_json, timestamps
approvals        -- id, kind, payload_json, status, requested_at, decided_at
google_tokens    -- access, refresh, scope, expiry  (single row)
state_records    -- namespace/id/payload_json  (weather location, prefs)
audit_log        -- append-only. never UPDATE, never DELETE.
worker_state     -- worker_name, port, pid, status, last_swap_at
```

`artifacts_json` on `messages` is what makes the UI chat-native: a weather card, calendar agenda, or tool result is attached to the message that produced it and rendered inline, rather than living in a separate dashboard panel.

---

## 7. Voice — and why not the Web Speech API

The obvious path is `webkitSpeechRecognition` in the browser. I'm deliberately not taking it. Support on iOS Safari is inconsistent and historically unreliable, it silently varies across browser versions, and it ships your audio to a cloud speech service on some platforms — which defeats the point of a local assistant.

**Instead: `MediaRecorder` captures audio in the browser → POST the blob to Core → Omni transcribes it natively.**

This is the better path here specifically because Omni's audio capability was already validated on this exact hardware — the controlled test ("code 4827, color purple, action save settings") returned all three correctly, 3/3, and an earlier test with code 7319 also passed clean. That's a proven local capability, no cloud dependency, consistent behavior across every browser that can record audio.

**The catch, and it's the same asymmetry that drove the base-model decision: voice input only works while Omni is the active worker.** Coder cannot process audio. When Coder is swapped in, the mic button must visibly disable itself and explain why, with a one-tap offer to swap back to Omni. The same rule applies to any image/vision input. This is honest UI, not a limitation to paper over.

**Voice output** uses browser `SpeechSynthesis` — universally supported, no backend cost, and it drives the avatar's `speaking` state naturally via its `onstart`/`onend` events.

**Secure context is mandatory.** `getUserMedia` requires HTTPS or literal `localhost`. This is precisely why Tailscale `serve` (which provisions a real cert) is load-bearing rather than a nice-to-have — over plain HTTP from your phone, the mic would simply never prompt.

---

## 8. Auth and remote access

**One Google OAuth flow, two jobs.** Scopes requested together:

```
openid, email, profile                              → identity
https://www.googleapis.com/auth/calendar.readonly   → calendar read
https://www.googleapis.com/auth/calendar.events     → gated writes
```

`access_type=offline` and `prompt=consent` to guarantee a refresh token — without it the integration dies silently in an hour.

**Sole-owner binding.** The first successful login writes its Google `sub` claim into the `owner` table. Every subsequent login must match that exact `sub` or is rejected outright. Not a role check, not an allowlist — a single immutable identity. No registration flow exists to exploit.

**Redirect URIs** — register both in Google Cloud Console, since you'll use both:
```
http://127.0.0.1:8100/auth/callback              (desktop, local)
https://omega.<your-tailnet>.ts.net/auth/callback (phone, remote)
```

**Session cookies:** `HttpOnly`, `SameSite=Lax`, `Secure` when served over HTTPS.

**Tailscale topology:** Core binds `127.0.0.1:8100` only — it is never directly network-exposed. `tailscale serve` terminates TLS and proxies to it. Only devices on your tailnet can reach it.

> Use `tailscale serve`, **not** `tailscale funnel`. `serve` is tailnet-private. `funnel` publishes to the open internet — which would put your operator core, with its file and shell reach into Omega, behind nothing but a login form. Confirm the exact `serve` invocation against your installed Tailscale version; the CLI syntax has shifted across releases.

---

## 9. UI design

**Mobile-first, genuinely.** Layout designed at ~390px and scaled up — not a desktop layout with breakpoints bolted on. The 3440×1440 desktop view is the wide variant of a phone-native design, which is the right way round given you'll reach for the phone more often than not.

**Chat-native rendering.** There is one primary surface: the conversation. Weather, calendar agenda, tool results, generated images, swap notices — all render as cards *inside* the message stream, attached to the message that produced them. No separate dashboard tabs to navigate. Asking about the weather returns a weather card in the chat; asking about your day returns an agenda card in the chat.

**Avatar is a first-class element, not decoration.** Video loop, always present — centered above the stream on mobile, docked in a side rail on desktop. States:

| State | Trigger | Treatment |
|---|---|---|
| `idle` | default | steady frame, soft border |
| `listening` | mic recording | pulsing accent ring |
| `thinking` | request in flight | animated glow |
| `speaking` | TTS playing | brighter frame + rhythmic glow |
| `swapping` | model swap in progress | distinct amber state + visible progress |

The `swapping` state matters more than the others. A 15–20s silent pause reads as a crash; a 15–20s pause with an avatar visibly in transition and a "switching to Coder…" caption reads as the system working. The UI must always name which worker is active.

**Palette:** XV11-derived — `#000913` / `#010e1e` field, `#041428` panels, `#2878ff` and `#76a9fa` blue accents, `#35e88a` success, `#ffb84d` warning, `#ff5d5d` danger. Explicitly *not* XV12's current colors.

**PWA:** manifest + service worker so it installs to your home screen and opens chrome-free like a native app. Respect iOS safe-area insets so the composer isn't eaten by the home indicator. 44px minimum touch targets.

---

## 10. Security

- Core binds loopback only. All external reach goes through Tailscale.
- Sole-owner identity binding — no registration path, no second account possible.
- Every tool call passes the capability gateway before executing. Tiers: `read_only` (auto), `confirm_required` (explicit approval), `blocked` (never). Unlisted tools fail **closed**.
- The model never self-authorizes. Model output is treated as a *request* to run a tool, never as permission to run it.
- Destructive operations — file writes, deletions, shell execution, calendar writes — require explicit approval, surfaced as an approval card in chat.
- Secrets live in `config\.env.local`, gitignored, loaded as environment variables. Never in prompts, never in logs, never in the audit trail.
- Audit log is append-only.
- Swap completion requires *both* an `nvidia-smi` VRAM confirmation and a health-check 200 — never just a clean process exit, which can leave an orphaned CUDA context holding memory.
- Worker crash detection: failed health check triggers one relaunch attempt, then surfaces an honest error rather than hanging.

---

## 11. Build phases

**Phase 1 — Core skeleton + swap**
Config, SQLite schema, model router (the validated primitive), worker configs, `/health` and `/status`. Verify: Omni starts on boot, swap to Coder and back works end to end, timings match the ~15–20s baseline.

**Phase 2 — Chat loop**
OpenAI-compatible streaming client, WebSocket chat endpoint, conversation persistence, system prompt assembly, context management against the 32K budget. Verify: real conversation with history surviving a swap mid-thread.

**Phase 3 — Auth + remote**
Google OAuth, sole-owner binding, sessions, Tailscale serve. Verify: log in from the phone over HTTPS, second Google account correctly rejected.

**Phase 4 — UI shell**
React + Vite, theme, avatar with all five states, chat stream, composer, PWA manifest. Verify: installs to home screen, avatar states track real backend events, swap is legible rather than looking like a freeze.

**Phase 5 — Cards**
Weather (Open-Meteo, no key) and Calendar (live Google) rendering inline in chat. Verify: both render on phone and desktop, calendar writes correctly blocked behind approval.

**Phase 6 — Voice**
MediaRecorder capture → Core → Omni transcription; SpeechSynthesis output wired to avatar speaking state; mic correctly disabled with explanation while Coder is active. Verify: voice round-trip on the phone over Tailscale HTTPS.

**Phase 7 — Tools**
Capability gateway with a real starter set (read file, list directory, web search), approval cards for anything destructive.

Each phase is independently runnable and testable. No phase requires the next one to work.

---

## 12. Explicitly excluded from v1

- Finance / invoicing / financial reporting
- Security camera (RTSP/IP feed) integration
- PC webcam capture
- Image generation (ComfyUI) — deferred; possible later as temporary-load service after unloading the text worker
- Automatic model routing — manual `/coder` and `/omni` only in v1
- Multi-user anything
- Small-model tiering (SmolVLM2-2.2B, whisper.cpp as cheap swap targets) — still a legitimate optimization since a 2–3GB model should swap far faster than 15–20s, but not load-bearing now that full swap cost is known-tolerable. Revisit if multimodal interrupts prove frequent in real use.

---

## 13. Risks and what I cannot verify from here

**I am writing this in a Linux sandbox with no GPU and no shell access to Omega.** I can write and syntax-check code; I cannot run it against your hardware. Nothing in this plan should be treated as "working" until you've run it on Omega. I'll say plainly which parts are verified and which aren't as we go.

Specific risks worth naming up front:

- **`psutil.net_connections` on Windows** may need elevated rights to see socket ownership for all processes. If PID-by-port lookup returns nothing while a worker is clearly listening, that's the cause — Core may need to run as Administrator.
- **Tailscale `serve` CLI syntax varies by version.** Confirm against your installed release rather than trusting a command from memory.
- **Google OAuth redirect URIs must match exactly.** The Tailnet hostname must be registered before remote login will work.
- **Mobile Safari audio recording** has quirks around codec and user-gesture requirements. MediaRecorder is far more reliable than Web Speech API here, but it still needs testing on your actual phone, not assumed.
- **32K context management** — with long conversations plus tool results plus system prompt, context pressure is real. Phase 2 needs a deliberate compaction strategy, not an afterthought.

---

## 14. Open items

1. **"finances plan"** — in "delete finances plan security camera and pc camera," I read four exclusions: finance, planning/builder, security camera, PC camera. It may instead be three, with "finances plan" meaning *financial planning* as one item. My working assumption: **X Omni keeps a light task/agenda surface** (tasks and reminders alongside calendar), and drops only the finance/invoicing domain. Correct me if planning should go entirely.
2. **Avatar assets** — reuse the existing `xoduz-idle.mp4` / `xoduz-speaking.mp4`, or new ones for X Omni?
3. **Assistant name** — is it still "XODUZ" speaking through X Omni, or a new identity?
