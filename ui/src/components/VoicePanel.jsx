import { Play, X } from "lucide-react";

/**
 * Voice settings. Two independent choices:
 *   - which engine turns your speech into text
 *   - which voice reads replies back
 */
export default function VoicePanel({ voice, onClose }) {
  const enUS = voice.voices.filter((v) => v.lang === "en-US");
  const others = voice.voices.filter((v) => v.lang !== "en-US");

  return (
    <div className="voice-panel-backdrop" onClick={onClose}>
      <div className="voice-panel" onClick={(e) => e.stopPropagation()}>
        <div className="voice-panel-head">
          <strong>Voice</strong>
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            <X size={16} />
          </button>
        </div>

        {/* ---- speech to text ---- */}
        <label className="voice-field">
          <span>Speech to text</span>
          <select
            value={voice.sttMode}
            onChange={(e) => voice.setSttMode(e.target.value)}
          >
            <option value="browser" disabled={!voice.browserSttAvailable}>
              Chrome / Google {voice.browserSttAvailable ? "" : "(unavailable)"}
            </option>
            <option value="local" disabled={!voice.localSttAvailable}>
              Local — Omni {voice.localSttAvailable ? "" : "(unavailable)"}
            </option>
          </select>
        </label>
        <p className="voice-note">
          {voice.sttMode === "browser"
            ? "Browser/Google STT: Chrome sends microphone audio to Google over the internet for recognition. It is not local."
            : "Local STT: Omni transcribes the audio on Omega. The audio stays on this machine. It needs the Omni worker; Coder must switch first (~15–20s)."}
        </p>

        {/* ---- text to speech ---- */}
        <label className="voice-field">
          <span>Spoken replies</span>
          <select
            value={voice.voiceName}
            onChange={(e) => voice.setVoiceName(e.target.value)}
          >
            {enUS.length > 0 && (
              <optgroup label="English (US)">
                {enUS.map((v) => (
                  <option key={v.name} value={v.name}>
                    {v.name}
                  </option>
                ))}
              </optgroup>
            )}
            {others.length > 0 && (
              <optgroup label="Other">
                {others.map((v) => (
                  <option key={v.name} value={v.name}>
                    {v.name} — {v.lang}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        </label>

        <button
          className="voice-preview"
          onClick={() => voice.previewVoice(voice.voiceName)}
          disabled={!voice.voiceName}
        >
          <Play size={13} /> Preview
        </button>

        {voice.voices.length === 0 && (
          <p className="voice-note">
            No voices reported yet. Chrome loads them a moment after the page
            opens — close this and reopen it.
          </p>
        )}

        {!voice.secureContext && (
          <p className="voice-note error">
            The microphone needs HTTPS or localhost. Over plain HTTP it will
            never prompt.
          </p>
        )}
      </div>
    </div>
  );
}
