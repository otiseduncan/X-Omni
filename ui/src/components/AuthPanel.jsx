import { useEffect, useRef, useState } from "react";
import {
  Ban,
  Bell,
  BellOff,
  Copy,
  ExternalLink,
  KeyRound,
  Loader2,
  LogIn,
  LogOut,
  QrCode,
  RotateCcw,
  ShieldCheck,
  UserPlus,
  X,
} from "lucide-react";

import {
  authPanelMode,
  buildGoogleAuthSetupPayload,
  LOCAL_GOOGLE_CALLBACK,
} from "../lib/authSetup.js";
import {
  pushSupported,
  subscriptionPayload,
  urlBase64ToUint8Array,
} from "../lib/pushNotifications.js";

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
  const [testUsers, setTestUsers] = useState([]);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteUrl, setInviteUrl] = useState("");
  const [pushState, setPushState] = useState("unsupported");
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

  useEffect(() => {
    if (!canLogout || auth?.current_user?.role !== "owner") return;
    let active = true;
    fetch("/api/auth/admin/test-users", { credentials: "include", cache: "no-store" })
      .then(readResponse)
      .then((payload) => {
        if (active) setTestUsers(Array.isArray(payload.users) ? payload.users : []);
      })
      .catch((requestError) => {
        if (active) setError(`Could not load authorized users: ${requestError.message}`);
      });
    return () => { active = false; };
  }, [canLogout, auth?.current_user?.role]);

  useEffect(() => {
    if (!canLogout || !pushSupported()) return;
    if (Notification.permission === "denied") {
      setPushState("denied");
      return;
    }
    let active = true;
    navigator.serviceWorker.ready
      .then((registration) => registration.pushManager.getSubscription())
      .then((subscription) => {
        if (active) setPushState(subscription ? "subscribed" : "available");
      })
      .catch(() => {
        if (active) setPushState("available");
      });
    return () => { active = false; };
  }, [canLogout]);

  async function enableNotifications() {
    setBusy(true);
    setError("");
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        setPushState("denied");
        return;
      }
      const keyResponse = await fetch("/api/push/public-key", { credentials: "include" });
      const { key } = await readResponse(keyResponse);
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key),
      });
      const response = await fetch("/api/push/subscribe", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(subscriptionPayload(subscription)),
      });
      await readResponse(response);
      setPushState("subscribed");
    } catch (requestError) {
      setError(`Could not enable notifications: ${requestError.message}`);
    } finally {
      setBusy(false);
    }
  }

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

  async function inviteTester(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/auth/admin/test-users", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: inviteEmail.trim(),
          tailscale_invite_url: inviteUrl.trim() || null,
        }),
      });
      const user = await readResponse(response);
      setTestUsers((items) => [user, ...items.filter((item) => item.id !== user.id)]);
      setInviteEmail("");
      setInviteUrl("");
    } catch (requestError) {
      setError(`Could not authorize tester: ${requestError.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function changeTester(userId, status) {
    setBusy(true);
    setError("");
    try {
      const response = await fetch(`/api/auth/admin/test-users/${encodeURIComponent(userId)}`, {
        method: "PATCH",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      const user = await readResponse(response);
      setTestUsers((items) => items.map((item) => (item.id === user.id ? user : item)));
    } catch (requestError) {
      setError(`Could not update tester: ${requestError.message}`);
    } finally {
      setBusy(false);
    }
  }

  async function copyInvite(url) {
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      setError("Could not copy the invitation URL. Select and copy it manually.");
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
                ? `${auth?.current_user?.role === "owner" ? "Owner" : "Test user"} session`
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
              Signed in as <strong>{auth?.current_user?.display_name || auth?.current_user?.email}</strong>
              {auth?.current_user?.email ? <> · {auth.current_user.email}</> : null}. Signing
              out removes only this browser session.
            </p>
            {auth?.current_user?.role === "owner" && (
              <section className="authorized-users" aria-labelledby="authorized-users-title">
                <div className="authorized-users-head">
                  <div>
                    <span>Remote access</span>
                    <strong id="authorized-users-title">Authorized test users</strong>
                  </div>
                  <ShieldCheck size={20} />
                </div>
                <form className="tester-invite-form" onSubmit={inviteTester}>
                  <label className="auth-field">
                    <span>Tester email</span>
                    <input
                      type="email"
                      value={inviteEmail}
                      onChange={(event) => setInviteEmail(event.target.value)}
                      placeholder="testuser@example.com"
                      required
                    />
                  </label>
                  <label className="auth-field">
                    <span>Tailscale invitation URL <em>optional</em></span>
                    <input
                      type="url"
                      value={inviteUrl}
                      onChange={(event) => setInviteUrl(event.target.value)}
                      placeholder="https://login.tailscale.com/a/..."
                    />
                  </label>
                  <button className="auth-primary" type="submit" disabled={busy || !inviteEmail.trim()}>
                    {busy ? <Loader2 size={16} className="spin" /> : <UserPlus size={16} />}
                    Authorize tester
                  </button>
                </form>
                <div className="tester-list">
                  {testUsers.length === 0 ? (
                    <p className="auth-private-note">No test users are authorized yet.</p>
                  ) : testUsers.map((user) => (
                    <article className="tester-card" key={user.id}>
                      <div className="tester-card-head">
                        <strong>{user.display_name || "Test User"}</strong>
                        <span className={`tester-status ${user.status}`}>{user.status}</span>
                      </div>
                      <dl>
                        <div><dt>Email</dt><dd>{user.email}</dd></div>
                        <div><dt>Role</dt><dd>Test User</dd></div>
                        <div><dt>Tailscale</dt><dd>{user.tailscale_verified ? user.tailscale_login : "Pending"}</dd></div>
                        <div><dt>Google</dt><dd>{user.google_verified ? "Verified" : "Pending"}</dd></div>
                        <div><dt>X Profile</dt><dd>{user.profile_created ? "Created" : "Pending"}</dd></div>
                        <div><dt>Last Login</dt><dd>{user.last_login_at || "Never"}</dd></div>
                      </dl>
                      {user.tailscale_invite_url && (
                        <div className="tester-qr">
                          <img
                            src={`/api/auth/admin/test-users/${encodeURIComponent(user.id)}/invite-qr.png`}
                            alt={`Tailscale invitation QR for ${user.email}`}
                          />
                          <button type="button" className="auth-secondary" onClick={() => copyInvite(user.tailscale_invite_url)}>
                            <Copy size={14} /> Copy invite
                          </button>
                          <a className="auth-secondary" href={`/api/auth/admin/test-users/${encodeURIComponent(user.id)}/invite-qr.png`} download>
                            <QrCode size={14} /> Save QR
                          </a>
                        </div>
                      )}
                      <div className="tester-actions">
                        {user.status === "revoked" ? (
                          <button type="button" className="auth-secondary" disabled={busy} onClick={() => changeTester(user.id, "pending")}>
                            <RotateCcw size={14} /> Require re-enrollment
                          </button>
                        ) : (
                          <button type="button" className="auth-secondary danger" disabled={busy} onClick={() => changeTester(user.id, "revoked")}>
                            <Ban size={14} /> Revoke X access
                          </button>
                        )}
                      </div>
                    </article>
                  ))}
                </div>
                <p className="auth-private-note">
                  Revoking X access invalidates X sessions. Removing a device or user from
                  the tailnet is a separate Tailscale administrator action.
                </p>
              </section>
            )}
            {pushState !== "unsupported" && (
              <div className="auth-notifications">
                {pushState === "subscribed" ? (
                  <p className="auth-private-note">
                    <Bell size={14} /> Notifications are enabled on this device.
                  </p>
                ) : pushState === "denied" ? (
                  <p className="auth-private-note">
                    <BellOff size={14} /> Notifications are blocked in the browser's site settings.
                  </p>
                ) : (
                  <button
                    type="button"
                    className="auth-secondary"
                    onClick={enableNotifications}
                    disabled={busy}
                  >
                    <Bell size={14} /> Enable notifications
                  </button>
                )}
              </div>
            )}
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
