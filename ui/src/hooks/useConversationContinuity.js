import { useCallback, useEffect, useRef, useState } from "react";

import {
  mergeTimelines,
  timelineFromHistory,
  upsertTimelineItem,
  updateApproval,
} from "../lib/conversationTimeline.js";

const ACTIVE_CONVERSATION_KEY = "xomni.activeConversationId";

function clientKey() {
  if (globalThis.crypto?.randomUUID) return `client:${globalThis.crypto.randomUUID()}`;
  return `client:${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function responseJson(response) {
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || body.message || `HTTP ${response.status}`);
  }
  return body;
}

async function reconcileApprovalStates(timeline, conversationId) {
  const unresolved = timeline.filter(
    (item) => item.kind === "approval" && item.approval?.id && !item.receipt
  );
  if (!unresolved.length) return timeline;

  const records = await Promise.all(
    unresolved.map(async (item) => {
      try {
        const query = new URLSearchParams({ conversation_id: String(conversationId) });
        const response = await fetch(`/api/approvals/${encodeURIComponent(item.approval.id)}?${query}`, {
          credentials: "include",
          cache: "no-store",
        });
        if (!response.ok) return null;
        return await response.json();
      } catch {
        return null;
      }
    })
  );

  return records.reduce((items, record) => {
    if (!record) return items;
    const approval = record.approval || record;
    const receipt = record.receipt || approval.receipt || null;
    const id = approval.id || receipt?.approval_id;
    if (!id) return items;
    return updateApproval(items, id, {
      status: approval.status || receipt?.status,
      receipt,
      approval,
    });
  }, timeline);
}

/**
 * Owns the durable chat timeline. Initial load selects the server's latest
 * conversation; reconnects merge authoritative history over live optimistic
 * entries so persisted messages/artifacts win without appearing twice.
 */
export function useConversationContinuity({ enabled }) {
  const [items, setItems] = useState([]);
  const [conversationId, setConversationId] = useState(null);
  const [ready, setReady] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const requestRef = useRef(0);
  const conversationIdRef = useRef(null);

  const adoptConversation = useCallback((id) => {
    const numericId = id == null ? null : Number(id);
    const next = Number.isFinite(numericId) ? numericId : id;
    conversationIdRef.current = next;
    setConversationId(next);
    try {
      if (next == null) window.localStorage.removeItem(ACTIVE_CONVERSATION_KEY);
      else window.localStorage.setItem(ACTIVE_CONVERSATION_KEY, String(next));
    } catch {
      // Storage can be disabled; server history remains authoritative.
    }
  }, []);

  const push = useCallback((item) => {
    const withKey = { ...item, key: item.key || clientKey() };
    setItems((previous) => upsertTimelineItem(previous, withKey));
    return withKey.key;
  }, []);

  const loadMessages = useCallback(async (id, { merge = false } = {}) => {
    const response = await fetch(`/api/conversations/${id}/messages`, {
      credentials: "include",
      cache: "no-store",
    });
    const payload = await responseJson(response);
    const authoritative = await reconcileApprovalStates(timelineFromHistory(payload), id);
    setItems((live) => (merge ? mergeTimelines(authoritative, live) : authoritative));
    return authoritative;
  }, []);

  const restoreLatest = useCallback(async ({ merge = false } = {}) => {
    const request = ++requestRef.current;
    setRestoring(true);
    try {
      const response = await fetch("/api/conversations", {
        credentials: "include",
        cache: "no-store",
      });
      const payload = await responseJson(response);
      const conversations = Array.isArray(payload) ? payload : payload.conversations || [];
      const latest = conversations[0] || null;
      if (request !== requestRef.current) return null;

      if (!latest) {
        adoptConversation(null);
        if (!merge) setItems([]);
        return null;
      }

      adoptConversation(latest.id);
      await loadMessages(latest.id, { merge });
      return latest.id;
    } finally {
      if (request === requestRef.current) {
        setReady(true);
        setRestoring(false);
      }
    }
  }, [adoptConversation, loadMessages]);

  useEffect(() => {
    if (!enabled) {
      requestRef.current += 1;
      setReady(false);
      setRestoring(false);
      return undefined;
    }

    let active = true;
    restoreLatest().catch((error) => {
      if (!active) return;
      setReady(true);
      setRestoring(false);
      push({ kind: "error", text: `Could not restore the latest conversation: ${error.message}` });
    });
    return () => {
      active = false;
      requestRef.current += 1;
    };
  }, [enabled, push, restoreLatest]);

  const reconcile = useCallback(async () => {
    const id = conversationIdRef.current;
    try {
      if (id == null) return await restoreLatest({ merge: true });
      setRestoring(true);
      await loadMessages(id, { merge: true });
      return id;
    } catch (error) {
      push({ kind: "error", text: `Could not reconcile conversation history: ${error.message}` });
      return null;
    } finally {
      setRestoring(false);
    }
  }, [loadMessages, push, restoreLatest]);

  const createConversation = useCallback(async () => {
    const response = await fetch("/api/conversations", {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
    });
    const payload = await responseJson(response);
    const id = payload.id ?? payload.conversation_id;
    if (id == null) throw new Error("Core did not return a conversation ID.");
    requestRef.current += 1;
    adoptConversation(id);
    setItems([]);
    return id;
  }, [adoptConversation]);

  return {
    items,
    setItems,
    push,
    conversationId,
    conversationIdRef,
    adoptConversation,
    ready,
    restoring,
    reconcile,
    createConversation,
  };
}
