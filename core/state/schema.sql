-- X Omni state store. SQLite, WAL mode (set by db.py at connect time).

-- Exactly one owner, ever. Bound to a Google `sub` claim on first login.
CREATE TABLE IF NOT EXISTS owner (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    google_sub  TEXT NOT NULL UNIQUE,
    email       TEXT,
    display_name TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    google_sub  TEXT NOT NULL,
    user_agent  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT,
    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user','assistant','system','tool')),
    content         TEXT NOT NULL,
    worker_used     TEXT,
    -- Cards rendered inline in the chat stream (weather, agenda, tool
    -- results). JSON array; '[]' when none.
    artifacts_json  TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    message_id      INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    approval_id     TEXT,
    tool_call_id    TEXT,
    tool_name       TEXT NOT NULL,
    args_json       TEXT NOT NULL,
    result_json     TEXT,
    result_hash     TEXT,
    action_digest   TEXT,
    idempotency_key TEXT,
    status          TEXT NOT NULL DEFAULT 'succeeded'
                      CHECK (status IN ('pending','executing','succeeded','failed','denied','expired','blocked')),
    approved_by     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_conversation ON tool_calls(conversation_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_calls_approval
    ON tool_calls(approval_id) WHERE approval_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','in_progress','done','abandoned')),
    due_at          TEXT,
    plan_json       TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    summary         TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    args_json       TEXT NOT NULL,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    session_id      TEXT,
    user_id         TEXT,
    message_id      INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    tool_call_id    TEXT NOT NULL,
    action_digest   TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','executing','succeeded','failed','denied','expired')),
    requested_at    TEXT NOT NULL DEFAULT (datetime('now')),
    started_at      TEXT,
    completed_at    TEXT,
    decided_at      TEXT,
    execution_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_context
    ON approvals(conversation_id, message_id, requested_at);

-- Immutable terminal evidence for a protected action. The application only
-- INSERTs here; replay reads this row instead of invoking the tool again.
CREATE TABLE IF NOT EXISTS execution_receipts (
    id              TEXT PRIMARY KEY,
    approval_id     TEXT NOT NULL UNIQUE REFERENCES approvals(id) ON DELETE CASCADE,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
    message_id      INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    tool_call_id    TEXT NOT NULL,
    tool_name       TEXT NOT NULL,
    action_digest   TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status          TEXT NOT NULL
                      CHECK (status IN ('succeeded','failed','denied','expired')),
    executed        INTEGER NOT NULL CHECK (executed IN (0,1)),
    success         INTEGER NOT NULL CHECK (success IN (0,1)),
    result_json     TEXT,
    result_hash     TEXT NOT NULL,
    error           TEXT,
    requested_at    TEXT NOT NULL,
    started_at      TEXT,
    completed_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_execution_receipts_action
    ON execution_receipts(action_digest, completed_at);

-- Single row. access_token/refresh_token for Google.
CREATE TABLE IF NOT EXISTS google_tokens (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    access_token  TEXT,
    refresh_token TEXT,
    scope         TEXT,
    expires_at    INTEGER,
    account_email TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Generic namespaced key/value for small state (weather location, prefs).
CREATE TABLE IF NOT EXISTS state_records (
    namespace    TEXT NOT NULL,
    id           TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (namespace, id)
);

-- Append-only. Nothing in this codebase may UPDATE or DELETE from here.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    detail_json TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS worker_state (
    worker_name  TEXT PRIMARY KEY,
    port         INTEGER,
    pid          INTEGER,
    status       TEXT,
    last_swap_at TEXT
);
