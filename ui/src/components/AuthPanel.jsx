import { useEffect, useRef, useState } from "react";
import { ExternalLink, KeyRound, Loader2, LogIn, LogOut, X } from "lucide-react";

import {
  authPanelMode,
  buildGoogleAuthSetupPayload,
  LOCAL_GOOGLE_CALLBACK,
} from "../lib/authSetup.js";

async function readResponse(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || payload.message || `HTTP ${response.status}`);
  }
  return payload;
}

export default function AuthPanel({ auth, onClose, onLoggedOut }) {
  const mode = authPanelMode(auth);
  const canLogout = mode === "session";
  const configurationReady = mode === "ready";
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [publicOrigin, setPublicOrigin] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const firstFieldRef = useRef(null);
  const closeRef = useRef(null);

  useEffect(() => {
    (firstFieldRef.current || closeRef.current)?.focus();
    const onKeyDown = (event) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  async function logout() {
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/auth/logout", {
        method: "POST",
        credentials: "include",
      });
      await readResponse(response);
      onClose();
      await onLoggedOut();
    } catch (requestError) {
      setError(`Could not sign out: ${requestError.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function submitSetup(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setSaved(false);
    try {
      const payload = buildGoogleAuthSetupPayload({ clientId, clientSecret, publicOrigin });
      const response = await fetch("/api/auth/setup", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      await readResponse(response);
      // Never retain the secret after Core has accepted it.
      setClientSecret("");
      setSaved(true);
    } catch (requestError) {
      setError(`Could not save Google Auth setup: ${requestError.message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-panel-backdrop" onClick={onClose}>
      <section
        className="auth-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="auth-panel-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="auth-panel-head">
          <div>
            <span>Account</span>
            <strong id="auth-panel-title">
              {canLogout
                ? "Owner session"
                : configurationReady
                  ? "Google Auth ready"
                  : "Google Auth setup"}
            </strong>
          </div>
          <button
            ref={closeRef}
            className="icon-btn"
            onClick={onClose}
            aria-label="Close account panel"
          >
            <X size={17} />
          </button>
        </div>

        {canLogout ? (
          <div className="auth-session">
            <p>
              This browser has an authenticated Owner session. Signing out
              removes only this browser session.
            </p>
            <button className="auth-primary auth-logout" onClick={logout} disabled={busy}>
              {busy ? <Loader2 size={16} className="spin" /> : <LogOut size={16} />}
              Sign out
            </button>
          </div>
        ) : saved ? (
          <div className="auth-success" role="status">
            <KeyRound size={22} />
            <strong>Google Auth configuration saved</strong>
            <p>
              Restart X Omni Core to load it. This page will not restart the
              service automatically.
            </p>
            <button className="auth-primary" onClick={onClose}>Done</button>
          </div>
        ) : configurationReady ? (
          <div className="auth-ready" role="status">
            <KeyRound size={22} />
            <strong>Google Auth is configured</strong>
            <p>
              {auth?.owner_bound
                ? "Sign in with the Google account already bound as Owner."
                : "The first successful local Google sign-in will securely bind that account as this X Omni instance’s Owner."}
            </p>
            <div className="auth-callback">
              <span>Authorized redirect URI reminder</span>
              <code>{LOCAL_GOOGLE_CALLBACK}</code>
            </div>
            <a className="auth-primary" href="/api/auth/login">
              <LogIn size={16} />
              {auth?.owner_bound ? "Sign in with Google" : "Sign in with Google and become Owner"}
            </a>
            <button className="auth-secondary" type="button" onClick={onClose}>
              Close
            </button>
          </div>
        ) : (
          <form className="auth-setup-form" onSubmit={submitSetup}>
            <p className="auth-explainer">
              Create an OAuth 2.0 <strong>Web application</strong> in Google
              Cloud, then enter its credentials here. Setup is accepted only
              from this computer&apos;s loopback address.
            </p>

            <div className="auth-callback">
              <span>Authorized redirect URI</span>
              <code>{LOCAL_GOOGLE_CALLBACK}</code>
            </div>

            <a
              className="auth-cloud-link"
              href="https://console.cloud.google.com/apis/credentials"
              target="_blank"
              rel="noreferrer"
            >
              Open Google Cloud credentials <ExternalLink size={13} />
            </a>

            <label className="auth-field">
              <span>Google client ID</span>
              <input
                ref={firstFieldRef}
                type="text"
                value={clientId}
                onChange={(event) => setClientId(event.target.value)}
                autoComplete="off"
                spellCheck={false}
                placeholder="123456789.apps.googleusercontent.com"
                required
              />
            </label>

            <label className="auth-field">
              <span>Google client secret</span>
              <input
                type="password"
                value={clientSecret}
                onChange={(event) => setClientSecret(event.target.value)}
                autoComplete="off"
                data-1p-ignore="true"
                data-lpignore="true"
                spellCheck={false}
                minLength={8}
                required
              />
            </label>
            <p className="auth-private-note">
              The secret is sent once to local X Omni Core. It is not saved in
              browser storage or written to the browser console.
            </p>

            <label className="auth-field">
              <span>Public HTTPS origin <em>optional</em></span>
              <input
                type="url"
                value={publicOrigin}
                onChange={(event) => setPublicOrigin(event.target.value)}
                placeholder="https://omega.example.ts.net"
                autoComplete="off"
                spellCheck={false}
              />
            </label>

            <button
              className="auth-primary"
              type="submit"
              disabled={busy || !clientId.trim() || !clientSecret.trim()}
            >
              {busy ? <Loader2 size={16} className="spin" /> : <KeyRound size={16} />}
              Save Google Auth setup
            </button>
          </form>
        )}

        {error && <p className="auth-panel-error" role="alert">{error}</p>}
      </section>
    </div>
  );
}
