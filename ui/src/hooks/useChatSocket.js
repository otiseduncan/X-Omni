import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Chat WebSocket with auto-reconnect.
 *
 * Reconnect matters more than usual here: a model swap restarts a
 * process and can take 15-20s, and a phone backgrounding the PWA will
 * drop the socket outright. Neither should require a manual refresh.
 */
export function useChatSocket({ onEvent, enabled = true }) {
  const [connected, setConnected] = useState(false);
  // Surfaced so the UI can stop saying a hopeful "reconnecting…" forever
  // and admit Core probably isn't running.
  const [attempts, setAttempts] = useState(0);
  const [rejected, setRejected] = useState(false);
  const [connectionEpoch, setConnectionEpoch] = useState(0);
  const socketRef = useRef(null);
  const retryRef = useRef(0);
  const timerRef = useRef(null);
  const activeRef = useRef(false);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const connect = useCallback(() => {
    if (!enabled || !activeRef.current) return;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${window.location.host}/ws/chat`);
    socketRef.current = ws;

    ws.onopen = () => {
      if (!activeRef.current || socketRef.current !== ws) return;
      retryRef.current = 0;
      setAttempts(0);
      setRejected(false);
      setConnected(true);
      setConnectionEpoch((value) => value + 1);
    };

    ws.onmessage = (raw) => {
      try {
        onEventRef.current?.(JSON.parse(raw.data));
      } catch {
        /* ignore malformed frame */
      }
    };

    ws.onclose = (event) => {
      if (socketRef.current !== ws) return;
      setConnected(false);
      socketRef.current = null;
      if (!activeRef.current) return;
      // 4401 is a deliberate rejection, not a transport failure. Retrying
      // would just fail identically forever.
      if (event.code === 4401) {
        setRejected(true);
        onEventRef.current?.({ type: "unauthorized" });
        return;
      }
      // Backoff to 10s. Long enough not to hammer Core while a worker
      // is mid-swap, short enough to feel instant when it comes back.
      const delay = Math.min(1000 * 2 ** retryRef.current, 10000);
      retryRef.current += 1;
      setAttempts((n) => n + 1);
      timerRef.current = window.setTimeout(connect, delay);
    };

    ws.onerror = () => ws.close();
  }, [enabled]);

  useEffect(() => {
    activeRef.current = true;
    connect();
    return () => {
      activeRef.current = false;
      window.clearTimeout(timerRef.current);
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
      setConnected(false);
    };
  }, [connect]);

  const send = useCallback((payload) => {
    const ws = socketRef.current;
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }, []);

  return { connected, send, attempts, rejected, connectionEpoch };
}
