export const LOCAL_GOOGLE_CALLBACK = "http://127.0.0.1:8100/api/auth/callback";

export function authPanelMode(auth) {
  if (auth?.auth_enabled && auth?.signed_in) return "session";
  if (auth?.auth_enabled && auth?.google_configured) return "ready";
  return "setup";
}

export function buildGoogleAuthSetupPayload({ clientId, clientSecret, publicOrigin }) {
  const payload = {
    client_id: String(clientId || "").trim(),
    client_secret: String(clientSecret || "").trim(),
  };
  const origin = String(publicOrigin || "").trim().replace(/\/+$/, "");
  if (origin) payload.public_origin = origin;
  return payload;
}
