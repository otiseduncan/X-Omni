/** Pure helpers for Web Push subscription -- kept out of the component so
 * they're testable without a browser. */

export function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  return Uint8Array.from([...rawData].map((char) => char.charCodeAt(0)));
}

export function subscriptionPayload(subscription) {
  const json = subscription.toJSON();
  return {
    endpoint: json.endpoint || "",
    p256dh: json.keys?.p256dh || "",
    auth: json.keys?.auth || "",
  };
}

export function pushSupported() {
  return (
    typeof window !== "undefined" &&
    typeof navigator !== "undefined" &&
    "serviceWorker" in navigator &&
    "PushManager" in window &&
    "Notification" in window
  );
}
