import { useCallback, useEffect, useRef, useState } from "react";

import { toSpeechText } from "../lib/speechText.js";

/**
 * Voice I/O with two selectable speech-to-text engines.
 *
 *   "browser" — Chrome's SpeechRecognition. This is Google's recognizer:
 *               fast, accurate, live interim text, and it needs no model
 *               worker running. It is NOT local — Chrome streams the audio
 *               to Google. Default, because it actually works today.
 *
 *   "local"   — MediaRecorder -> POST /api/voice/transcribe -> Omni's
 *               native audio understanding. Nothing leaves Omega. Requires
 *               the Omni worker to be loaded, and costs a swap if Coder is
 *               active.
 *
 * Text-to-speech uses SpeechSynthesis with an explicit voice choice,
 * preferring "Google US English" and remembering the selection.
 *
 * Both need a secure context: HTTPS, or localhost. Over plain HTTP from a
 * phone the microphone never prompts at all.
 */

const STT_KEY = "xomni.sttMode";
const VOICE_KEY = "xomni.voiceName";
const BROWSER_END_SILENCE_MS = 1800;
const BROWSER_RESTART_DELAY_MS = 120;
const LOCAL_AUDIO_CONSTRAINTS = {
  channelCount: 1,
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
};

function readPref(key, fallback) {
  try {
    return window.localStorage.getItem(key) || fallback;
  } catch {
    return fallback;
  }
}

function writePref(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* private mode — preference just won't persist */
  }
}

/** Rank candidates so "Google US English" wins when Chrome offers it. */
function pickDefaultVoice(voices) {
  if (!voices.length) return null;
  const score = (v) => {
    const n = (v.name || "").toLowerCase();
    if (n === "google us english") return 100;
    if (n.startsWith("google") && v.lang === "en-US") return 90;
    if (n.startsWith("google") && v.lang?.startsWith("en")) return 80;
    if (n.startsWith("microsoft") && v.lang === "en-US") return 60;
    if (v.lang === "en-US") return 50;
    if (v.lang?.startsWith("en")) return 40;
    return 0;
  };
  return [...voices].sort((a, b) => score(b) - score(a))[0];
}

export function useVoice({ onTranscript, onSpeakingChange, onError, onInterim }) {
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [sttMode, setSttModeState] = useState(() => readPref(STT_KEY, "browser"));
  const [voices, setVoices] = useState([]);
  const [voiceName, setVoiceNameState] = useState(() => readPref(VOICE_KEY, ""));

  const recognitionRef = useRef(null);
  const browserSessionRef = useRef(null);
  const recorderRef = useRef(null);
  const chunksRef = useRef([]);
  const streamRef = useRef(null);
  const finalRef = useRef("");

  const cb = useRef({ onTranscript, onSpeakingChange, onError, onInterim });
  cb.current = { onTranscript, onSpeakingChange, onError, onInterim };

  const secureContext =
    typeof window !== "undefined" &&
    (window.isSecureContext ||
      ["localhost", "127.0.0.1"].includes(window.location.hostname));

  const SpeechRecognitionImpl =
    typeof window !== "undefined"
      ? window.SpeechRecognition || window.webkitSpeechRecognition
      : undefined;

  const browserSttAvailable = Boolean(SpeechRecognitionImpl) && secureContext;
  const recorderAvailable =
    typeof navigator !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof window.MediaRecorder !== "undefined" &&
    secureContext;

  const effectiveMode =
    sttMode === "browser" && !browserSttAvailable ? "local" : sttMode;
  const supported = browserSttAvailable || recorderAvailable;

  // ---------- voice list ----------
  useEffect(() => {
    if (typeof window.speechSynthesis === "undefined") return undefined;

    const load = () => {
      // getVoices() is empty on first call in Chrome until the engine
      // populates it and fires voiceschanged.
      const list = window.speechSynthesis.getVoices() || [];
      if (!list.length) return;
      setVoices(list);
      setVoiceNameState((current) => {
        if (current && list.some((v) => v.name === current)) return current;
        const best = pickDefaultVoice(list);
        if (best) writePref(VOICE_KEY, best.name);
        return best ? best.name : "";
      });
    };

    load();
    window.speechSynthesis.addEventListener("voiceschanged", load);
    return () => window.speechSynthesis.removeEventListener("voiceschanged", load);
  }, []);

  const setSttMode = useCallback((mode) => {
    setSttModeState(mode);
    writePref(STT_KEY, mode);
  }, []);

  const setVoiceName = useCallback((name) => {
    setVoiceNameState(name);
    writePref(VOICE_KEY, name);
  }, []);

  // ---------- STT: browser (Chrome / Google) ----------
  const startBrowserStt = useCallback(() => {
    if (!SpeechRecognitionImpl) return;

    finalRef.current = "";
    const session = {
      active: true,
      finishing: false,
      idleTimer: null,
      restartTimer: null,
      finalize: null,
    };
    browserSessionRef.current = session;

    const clearTimers = () => {
      if (session.idleTimer != null) window.clearTimeout(session.idleTimer);
      if (session.restartTimer != null) window.clearTimeout(session.restartTimer);
      session.idleTimer = null;
      session.restartTimer = null;
    };

    const finalize = () => {
      if (browserSessionRef.current !== session) return;
      session.active = false;
      session.finishing = true;
      clearTimers();
      browserSessionRef.current = null;
      recognitionRef.current = null;
      setRecording(false);
      const text = finalRef.current.trim();
      finalRef.current = "";
      cb.current.onInterim?.("");
      if (text) cb.current.onTranscript?.(text);
    };
    session.finalize = finalize;

    const requestFinishAfterSilence = () => {
      if (!session.active || session.finishing) return;
      if (session.idleTimer != null) window.clearTimeout(session.idleTimer);
      session.idleTimer = window.setTimeout(() => {
        if (!session.active || session.finishing) return;
        session.finishing = true;
        const recognition = recognitionRef.current;
        if (recognition) {
          try {
            recognition.stop();
          } catch {
            finalize();
          }
        } else {
          finalize();
        }
      }, BROWSER_END_SILENCE_MS);
    };

    const launch = () => {
      if (!session.active || session.finishing || browserSessionRef.current !== session) return;

      const recognition = new SpeechRecognitionImpl();
      recognitionRef.current = recognition;
      recognition.lang = "en-US";
      // Chrome can end an individual recognition stream after a short pause.
      // Continuous mode plus a guarded restart keeps the user's dictation
      // session alive until X's own silence grace period decides they are done.
      recognition.continuous = true;
      recognition.interimResults = true;
      recognition.maxAlternatives = 3;

      recognition.onresult = (event) => {
        let interim = "";
        for (let i = event.resultIndex; i < event.results.length; i += 1) {
          const chunk = event.results[i][0]?.transcript || "";
          if (event.results[i].isFinal) {
            finalRef.current += `${chunk} `;
          } else {
            interim += chunk;
          }
        }
        cb.current.onInterim?.(`${finalRef.current}${interim}`.trim());
        requestFinishAfterSilence();
      };

      recognition.onerror = (event) => {
        const map = {
          "not-allowed": "Microphone permission was denied.",
          "service-not-allowed": "Speech recognition was blocked by the browser.",
          network:
            "Chrome's speech recognition needs internet access. Switch STT to Local (Omni) to run offline.",
        };

        // Chrome routinely emits no-speech when it rotates a continuous
        // recognizer. That is not a reason to throw away the current session.
        if (event.error === "no-speech") return;
        if (event.error === "aborted" && (session.finishing || !session.active)) return;

        const message = map[event.error] || `Speech error: ${event.error}`;
        session.finishing = true;
        cb.current.onError?.(message);
      };

      recognition.onend = () => {
        if (recognitionRef.current === recognition) recognitionRef.current = null;
        if (session.finishing || !session.active) {
          finalize();
          return;
        }
        // Chrome may still rotate/end the recognizer itself. Restart quickly
        // while preserving accumulated final text and the silence timer.
        session.restartTimer = window.setTimeout(launch, BROWSER_RESTART_DELAY_MS);
      };

      try {
        recognition.start();
      } catch (err) {
        session.finishing = true;
        setRecording(false);
        browserSessionRef.current = null;
        recognitionRef.current = null;
        cb.current.onError?.(`Could not start recognition: ${err.message || err}`);
      }
    };

    setRecording(true);
    launch();
  }, [SpeechRecognitionImpl]);

  // ---------- STT: local (Omni) ----------
  const startLocalStt = useCallback(async () => {
    if (!recorderAvailable) {
      cb.current.onError?.(
        secureContext
          ? "This browser can't record audio."
          : "Microphone needs HTTPS. Use the Tailscale address, not plain HTTP."
      );
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: LOCAL_AUDIO_CONSTRAINTS,
      });
      streamRef.current = stream;
      chunksRef.current = [];

      const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", ""];
      const mimeType = candidates.find(
        (t) => !t || window.MediaRecorder.isTypeSupported(t)
      );
      const rec = new window.MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      recorderRef.current = rec;

      rec.ondataavailable = (e) => {
        if (e.data?.size > 0) chunksRef.current.push(e.data);
      };

      rec.onstop = async () => {
        recorderRef.current = null;
        const blob = new Blob(chunksRef.current, { type: rec.mimeType || "audio/webm" });
        chunksRef.current = [];
        streamRef.current?.getTracks().forEach((t) => t.stop());
        streamRef.current = null;
        setRecording(false);
        if (blob.size < 1200) {
          cb.current.onError?.("That recording was too short.");
          return;
        }
        setBusy(true);
        try {
          const form = new FormData();
          form.append("audio", blob, "clip.webm");
          const resp = await fetch("/api/voice/transcribe", {
            method: "POST",
            body: form,
            credentials: "include",
          });
          const payload = await resp.json().catch(() => ({}));
          if (!resp.ok) throw new Error(payload.detail || `Transcription failed (${resp.status}).`);
          if (payload.swapped) {
            cb.current.onError?.(
              `Switched to ${payload.worker} for audio (${payload.swapped.total_swap_s}s).`
            );
          }
          if (payload.text?.trim()) cb.current.onTranscript?.(payload.text.trim());
          else cb.current.onError?.("Nothing was transcribed.");
        } catch (err) {
          cb.current.onError?.(String(err.message || err));
        } finally {
          setBusy(false);
        }
      };

      // Timesliced chunks are more resilient on mobile browsers and avoid
      // losing the entire utterance if MediaRecorder stalls during stop().
      rec.start(250);
      setRecording(true);
    } catch (err) {
      cb.current.onError?.(
        err?.name === "NotAllowedError"
          ? "Microphone permission was denied."
          : `Could not start recording: ${err.message || err}`
      );
    }
  }, [recorderAvailable, secureContext]);

  const stop = useCallback(() => {
    const browserSession = browserSessionRef.current;
    if (browserSession) {
      browserSession.active = false;
      browserSession.finishing = true;
      if (browserSession.idleTimer != null) window.clearTimeout(browserSession.idleTimer);
      if (browserSession.restartTimer != null) window.clearTimeout(browserSession.restartTimer);
      const recognition = recognitionRef.current;
      if (recognition) {
        try {
          recognition.stop();
        } catch {
          browserSession.finalize?.();
        }
      } else {
        browserSession.finalize?.();
      }
      return;
    }

    const rec = recorderRef.current;
    if (rec && rec.state !== "inactive") {
      rec.stop();
      return;
    }
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    setRecording(false);
  }, []);

  const toggle = useCallback(() => {
    if (recording) {
      stop();
      return;
    }
    if (!supported) {
      cb.current.onError?.(
        secureContext
          ? "No speech input available in this browser."
          : "Microphone needs HTTPS or localhost."
      );
      return;
    }
    if (effectiveMode === "browser") startBrowserStt();
    else startLocalStt();
  }, [recording, stop, supported, secureContext, effectiveMode, startBrowserStt, startLocalStt]);

  useEffect(() => () => {
    const browserSession = browserSessionRef.current;
    if (browserSession) {
      browserSession.active = false;
      browserSession.finishing = true;
      if (browserSession.idleTimer != null) window.clearTimeout(browserSession.idleTimer);
      if (browserSession.restartTimer != null) window.clearTimeout(browserSession.restartTimer);
    }
    try {
      recognitionRef.current?.abort();
    } catch {
      /* already closed */
    }
    const rec = recorderRef.current;
    if (rec && rec.state !== "inactive") {
      try {
        rec.stop();
      } catch {
        /* already closed */
      }
    }
    streamRef.current?.getTracks().forEach((t) => t.stop());
  }, []);

  // ---------- TTS ----------
  const speak = useCallback(
    (text) => {
      if (!text || typeof window.speechSynthesis === "undefined") return;
      const spoken = toSpeechText(text);
      if (!spoken) return;
      window.speechSynthesis.cancel();
      const utter = new SpeechSynthesisUtterance(spoken.slice(0, 4000));
      const chosen = voices.find((v) => v.name === voiceName);
      if (chosen) {
        utter.voice = chosen;
        utter.lang = chosen.lang;
      }
      utter.rate = 1.02;
      utter.pitch = 1;
      utter.onstart = () => cb.current.onSpeakingChange?.(true);
      utter.onend = () => cb.current.onSpeakingChange?.(false);
      utter.onerror = () => cb.current.onSpeakingChange?.(false);
      window.speechSynthesis.speak(utter);
    },
    [voices, voiceName]
  );

  const stopSpeaking = useCallback(() => {
    window.speechSynthesis?.cancel();
    cb.current.onSpeakingChange?.(false);
  }, []);

  const previewVoice = useCallback(
    (name) => {
      const v = voices.find((x) => x.name === name);
      if (!v) return;
      window.speechSynthesis.cancel();
      const utter = new SpeechSynthesisUtterance("This is how I'll sound.");
      utter.voice = v;
      utter.lang = v.lang;
      utter.rate = 1.02;
      window.speechSynthesis.speak(utter);
    },
    [voices]
  );

  return {
    supported,
    secureContext,
    recording,
    busy,
    toggle,
    stop,
    speak,
    stopSpeaking,
    previewVoice,
    // settings surface
    sttMode: effectiveMode,
    setSttMode,
    browserSttAvailable,
    localSttAvailable: recorderAvailable,
    voices,
    voiceName,
    setVoiceName,
  };
}
