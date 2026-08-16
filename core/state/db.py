"""
X Omni -- state store.

SQLite in WAL mode. One connection, guarded by a lock, shared across the
app -- FastAPI runs handlers in a threadpool and async tasks on one loop,
so check_same_thread is off and every write goes through the lock.
"""

from __future__ import annotations

import json
import hashlib
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
APPROVAL_TTL = timedelta(hours=24)


# Kept here as well as schema.sql so an existing installation can replace the
# original approvals table before schema.sql creates tables that reference it.
_APPROVALS_SCHEMA_SQL = """
CREATE TABLE approvals (
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
)
"""

_SESSIONS_SCHEMA_SQL = """
CREATE TABLE sessions (
    token_hash  TEXT PRIMARY KEY,
    google_sub  TEXT NOT NULL,
    user_agent  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL
)
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utcnow().replace(microsecond=0).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class WebsiteRevisionConflict(RuntimeError):
    """The website lineage advanced before a child revision could commit."""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self._migrate_legacy_sessions()
        self._migrate_legacy_approvals()
        self._migrate_tool_calls()
        self.conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate_tool_calls()
        self.conn.commit()
        # An approval left in `executing` belongs to an interrupted prior
        # process. Its external side effect cannot be inferred safely after a
        # restart, so close it as indeterminate evidence instead of ever
        # making it claimable again.
        self.recover_interrupted_approvals()

    def _migrate_legacy_sessions(self) -> None:
        """Replace raw cookie storage with hashes; legacy cookies expire.

        Raw tokens cannot be safely converted while guaranteeing they are not
        retained in SQLite pages/WAL. Dropping the small session table forces a
        one-time sign-in after upgrade and removes them at the authoritative
        schema boundary.
        """
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        ).fetchone()
        if not row:
            return
        columns = {
            item["name"] for item in self.conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "token_hash" in columns and "token" not in columns:
            return
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute("PRAGMA secure_delete=ON")
                self.conn.execute("DROP TABLE sessions")
                self.conn.execute(_SESSIONS_SCHEMA_SQL)
                self.conn.commit()
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                self.conn.rollback()
                raise

    def _migrate_legacy_approvals(self) -> None:
        """Replace the prototype approval schema without making old, unbound
        approvals executable.

        SQLite cannot widen a CHECK constraint in place. Legacy pending or
        approved rows are therefore preserved as expired evidence. A protected
        action created by the new runtime always has complete identity binding.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'approvals'"
        ).fetchone()
        if not row:
            return
        sql = str(row["sql"] or "")
        columns = {
            item["name"] for item in self.conn.execute("PRAGMA table_info(approvals)").fetchall()
        }
        required = {
            "tool_name", "args_json", "conversation_id", "session_id", "user_id",
            "message_id", "tool_call_id", "action_digest", "idempotency_key",
            "started_at", "completed_at", "execution_error",
        }
        if required.issubset(columns) and "'executing'" in sql and "'succeeded'" in sql:
            return

        legacy_rows = [dict(item) for item in self.conn.execute("SELECT * FROM approvals")]
        with self._lock:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute("ALTER TABLE approvals RENAME TO approvals_legacy")
                self.conn.execute(_APPROVALS_SCHEMA_SQL)
                for item in legacy_rows:
                    try:
                        payload = json.loads(item.get("payload_json") or "{}")
                    except json.JSONDecodeError:
                        payload = {}
                    tool_name = str(payload.get("name") or item.get("kind") or "legacy_tool")
                    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
                    legacy_id = str(item.get("id") or secrets.token_urlsafe(12))
                    digest = f"legacy:{hashlib.sha256(legacy_id.encode()).hexdigest()}"
                    old_status = str(item.get("status") or "expired")
                    status = old_status if old_status in {"denied", "expired"} else "expired"
                    completed_at = item.get("decided_at") or _now_iso()
                    self.conn.execute(
                        """
                        INSERT INTO approvals (
                            id, kind, summary, payload_json, tool_name, args_json,
                            conversation_id, session_id, user_id, message_id,
                            tool_call_id, action_digest, idempotency_key, status,
                            requested_at, completed_at, decided_at, execution_error
                        ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            legacy_id, str(item.get("kind") or tool_name),
                            str(item.get("summary") or f"Legacy {tool_name} approval"),
                            _canonical_json(payload), tool_name, _canonical_json(args),
                            f"legacy_{legacy_id}", digest, f"legacy:{legacy_id}", status,
                            item.get("requested_at") or _now_iso(), completed_at, completed_at,
                            "Legacy approval expired during exact-once migration."
                            if status == "expired" else None,
                        ),
                    )
                self.conn.execute("DROP TABLE approvals_legacy")
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            finally:
                self.conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_tool_calls(self) -> None:
        """Add correlation columns to installations created by the prototype."""
        exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tool_calls'"
        ).fetchone()
        if not exists:
            return
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(tool_calls)").fetchall()
        }
        additions = {
            "conversation_id": "INTEGER REFERENCES conversations(id) ON DELETE CASCADE",
            "approval_id": "TEXT",
            "tool_call_id": "TEXT",
            "result_hash": "TEXT",
            "action_digest": "TEXT",
            "idempotency_key": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'succeeded'",
            "completed_at": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE tool_calls ADD COLUMN {name} {declaration}")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tool_calls_conversation "
            "ON tool_calls(conversation_id, id)"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_calls_approval "
            "ON tool_calls(approval_id) WHERE approval_id IS NOT NULL"
        )

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ---------- owner ----------

    def get_owner(self) -> Optional[dict]:
        row = self._one("SELECT * FROM owner WHERE id = 1")
        return dict(row) if row else None

    def bind_owner(self, google_sub: str, email: str, display_name: str) -> dict:
        """First successful login claims ownership permanently. Subsequent
        calls with a different sub are refused -- there is deliberately no
        path to re-bind from inside the app."""
        with self._lock:
            existing = self.conn.execute("SELECT * FROM owner WHERE id = 1").fetchone()
            if existing:
                if existing["google_sub"] != google_sub:
                    raise PermissionError("This X Omni instance is already bound to a different account.")
                return dict(existing)
            self.conn.execute(
                "INSERT INTO owner (id, google_sub, email, display_name) VALUES (1, ?, ?, ?)",
                (google_sub, email, display_name),
            )
            self.conn.commit()
            return dict(self.conn.execute("SELECT * FROM owner WHERE id = 1").fetchone())

    # ---------- sessions ----------

    @staticmethod
    def _session_token_hash(token: str) -> str:
        return hashlib.sha256(str(token).encode("utf-8")).hexdigest()

    def create_session(self, google_sub: str, user_agent: str, ttl_days: int) -> str:
        token = secrets.token_urlsafe(40)
        token_hash = self._session_token_hash(token)
        expires = (_utcnow() + timedelta(days=ttl_days)).isoformat()
        self._exec(
            "INSERT INTO sessions (token_hash, google_sub, user_agent, expires_at) VALUES (?, ?, ?, ?)",
            (token_hash, google_sub, user_agent[:300], expires),
        )
        return token

    def get_session(self, token: str) -> Optional[dict]:
        token_hash = self._session_token_hash(token)
        row = self._one("SELECT * FROM sessions WHERE token_hash = ?", (token_hash,))
        if not row:
            return None
        try:
            if datetime.fromisoformat(row["expires_at"]) < _utcnow():
                self.delete_session(token)
                return None
        except ValueError:
            return None
        return dict(row)

    def delete_session(self, token: str) -> None:
        self._exec(
            "DELETE FROM sessions WHERE token_hash = ?", (self._session_token_hash(token),)
        )

    # ---------- conversations & messages ----------

    def create_conversation(self, title: Optional[str] = None) -> int:
        return self._exec("INSERT INTO conversations (title) VALUES (?)", (title,)).lastrowid

    def list_conversations(self, limit: int = 50) -> list[dict]:
        rows = self._query(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    def conversation_exists(self, conversation_id: int) -> bool:
        return self._one(
            "SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)
        ) is not None

    def touch_conversation(self, conversation_id: int, title: Optional[str] = None) -> None:
        if title:
            self._exec(
                "UPDATE conversations SET updated_at = datetime('now'), "
                "title = COALESCE(title, ?) WHERE id = ?",
                (title, conversation_id),
            )
        else:
            self._exec(
                "UPDATE conversations SET updated_at = datetime('now') WHERE id = ?",
                (conversation_id,),
            )

    def add_message(
        self,
        conversation_id: int,
        role: str,
        content: str,
        worker_used: Optional[str] = None,
        artifacts: Optional[list] = None,
    ) -> int:
        mid = self._exec(
            "INSERT INTO messages (conversation_id, role, content, worker_used, artifacts_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, role, content, worker_used, json.dumps(artifacts or [])),
        ).lastrowid
        self.touch_conversation(conversation_id)
        return mid

    def add_website_revision_message(
        self,
        conversation_id: int,
        content: str,
        *,
        worker_used: Optional[str],
        artifacts: list,
        website_id: str,
        expected_parent_sha256: str,
    ) -> int:
        """Atomically append a website child only while its parent is head.

        Website HTML stays inside immutable message artifacts. The transaction
        scans the bounded conversation window under the same write lock used
        for insertion, preventing two concurrent edits from silently creating
        sibling revisions that both claim to be the latest preview.
        """
        lineage = str(website_id or "").strip()[:120]
        parent = str(expected_parent_sha256 or "").strip().casefold()
        if not lineage or not re.fullmatch(r"[0-9a-f]{64}", parent):
            raise ValueError("Website revision identity is invalid.")
        children = [
            artifact.get("data")
            for artifact in (artifacts or [])
            if isinstance(artifact, dict)
            and artifact.get("type") == "website_preview"
            and isinstance(artifact.get("data"), dict)
            and artifact["data"].get("ok") is True
        ]
        if len(children) != 1:
            raise ValueError("Website revision artifact is missing or ambiguous.")
        child = children[0]
        child_html = child.get("html")
        child_sha = str(child.get("sha256") or "").casefold()
        child_parent = str(child.get("parent_sha256") or "").casefold()
        if (
            child.get("website_id") != lineage
            or child_parent != parent
            or not isinstance(child_html, str)
            or not re.fullmatch(r"[0-9a-f]{64}", child_sha)
            or hashlib.sha256(child_html.encode("utf-8")).hexdigest() != child_sha
        ):
            raise ValueError("Website revision artifact does not match its commit identity.")

        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self.conn.execute(
                    "SELECT id, artifacts_json FROM messages "
                    "WHERE conversation_id = ? ORDER BY id DESC LIMIT 500",
                    (conversation_id,),
                ).fetchall()
                head_sha = None
                for row in rows:
                    try:
                        stored = json.loads(row["artifacts_json"] or "[]")
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(stored, list):
                        continue
                    for artifact in reversed(stored):
                        if not isinstance(artifact, dict) or artifact.get("type") != "website_preview":
                            continue
                        data = artifact.get("data")
                        if not isinstance(data, dict) or data.get("ok") is not True:
                            continue
                        digest = str(data.get("sha256") or "").casefold()
                        data_lineage = str(data.get("website_id") or "").strip()
                        if not data_lineage and re.fullmatch(r"[0-9a-f]{64}", digest):
                            data_lineage = f"website:{digest[:32]}"
                        if data_lineage == lineage:
                            head_sha = digest
                            break
                    if head_sha is not None:
                        break

                if head_sha != parent:
                    raise WebsiteRevisionConflict(
                        "The website preview changed before this revision could be saved."
                    )

                cursor = self.conn.execute(
                    "INSERT INTO messages "
                    "(conversation_id, role, content, worker_used, artifacts_json) "
                    "VALUES (?, 'assistant', ?, ?, ?)",
                    (
                        conversation_id,
                        content,
                        worker_used,
                        json.dumps(artifacts or []),
                    ),
                )
                self.conn.execute(
                    "UPDATE conversations SET updated_at = datetime('now') WHERE id = ?",
                    (conversation_id,),
                )
                self.conn.commit()
                return cursor.lastrowid
            except Exception:
                self.conn.rollback()
                raise

    def get_messages(self, conversation_id: int, limit: int = 500) -> list[dict]:
        rows = self._query(
            """
            SELECT * FROM (
                SELECT * FROM messages
                WHERE conversation_id = ?
                ORDER BY id DESC LIMIT ?
            ) AS newest
            ORDER BY id ASC
            """,
            (conversation_id, limit),
        )
        out = []
        for row in rows:
            d = dict(row)
            d["artifacts"] = json.loads(d.pop("artifacts_json") or "[]")
            out.append(d)
        return out

    # ---------- tool calls ----------

    def log_tool_call(
        self, message_id: Optional[int], tool_name: str, args: dict,
        result: Any = None, approved_by: Optional[str] = None,
        *, conversation_id: Optional[int] = None, approval_id: Optional[str] = None,
        tool_call_id: Optional[str] = None, action_digest: Optional[str] = None,
        idempotency_key: Optional[str] = None, status: str = "succeeded",
    ) -> int:
        result_json = json.dumps(result, default=str) if result is not None else None
        result_hash = _sha256_json(result) if result is not None else None
        return self._exec(
            """
            INSERT INTO tool_calls (
                conversation_id, message_id, approval_id, tool_call_id, tool_name,
                args_json, result_json, result_hash, action_digest, idempotency_key,
                status, approved_by, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id, message_id, approval_id, tool_call_id, tool_name,
                json.dumps(args, default=str), result_json, result_hash,
                action_digest, idempotency_key, status, approved_by,
                _now_iso() if status in {"succeeded", "failed", "denied", "expired", "blocked"} else None,
            ),
        ).lastrowid

    # ---------- approvals ----------

    @staticmethod
    def _approval_row(row: sqlite3.Row | None) -> Optional[dict]:
        if not row:
            return None
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json"))
        except (json.JSONDecodeError, TypeError):
            item["payload"] = {}
        try:
            item["args"] = json.loads(item.pop("args_json"))
        except (json.JSONDecodeError, TypeError):
            item["args"] = {}
        return item

    @staticmethod
    def _receipt_row(row: sqlite3.Row | None) -> Optional[dict]:
        if not row:
            return None
        item = dict(row)
        try:
            item["result"] = json.loads(item.pop("result_json")) if item.get("result_json") else None
        except (json.JSONDecodeError, TypeError):
            item["result"] = None
            item.pop("result_json", None)
        item["executed"] = bool(item["executed"])
        item["success"] = bool(item["success"])
        item["receipt_id"] = item["id"]
        result = item.get("result")
        if isinstance(result, dict) and (
            result.get("execution_state") == "indeterminate"
            or result.get("may_have_executed") is True
        ):
            item["execution_state"] = "indeterminate"
            item["may_have_executed"] = True
            item["outcome_message"] = str(
                result.get("message")
                or item.get("error")
                or "Execution may have started, but its outcome could not be verified."
            )
        return item

    def create_approval(
        self,
        kind: str,
        summary: str,
        payload: dict,
        *,
        conversation_id: int,
        session_id: str,
        user_id: str,
        message_id: int,
        tool_call_id: str,
        logged_args: Optional[dict] = None,
    ) -> str:
        """Create or reuse the one pending action for an exact bound identity."""
        tool_name = str(payload.get("name") or kind).strip()
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        session_id = str(session_id or "").strip()
        user_id = str(user_id or "").strip()
        tool_call_id = str(tool_call_id or "").strip()
        if not session_id or not user_id or not tool_call_id:
            raise ValueError("Approval identity is incomplete.")

        identity = {
            "version": 1,
            "conversation_id": int(conversation_id),
            "session_id": session_id,
            "user_id": user_id,
            "message_id": int(message_id),
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "args": args,
        }
        action_digest = _sha256_json(identity)
        idempotency_key = f"xomni:{action_digest}"
        approval_id = secrets.token_urlsafe(12)
        requested_at = _now_iso()
        audit_args = logged_args if logged_args is not None else dict(args)
        if tool_name == "write_file" and "content" in args and logged_args is None:
            content = str(args.get("content") or "")
            audit_args["content"] = {
                "redacted": True,
                "bytes": len(content.encode("utf-8")),
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }

        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                message = self.conn.execute(
                    "SELECT conversation_id FROM messages WHERE id = ?", (message_id,)
                ).fetchone()
                if not message or int(message["conversation_id"]) != int(conversation_id):
                    raise ValueError("Approval message does not belong to the conversation.")
                existing = self.conn.execute(
                    "SELECT id FROM approvals WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing:
                    self.conn.commit()
                    return str(existing["id"])
                self.conn.execute(
                    """
                    INSERT INTO approvals (
                        id, kind, summary, payload_json, tool_name, args_json,
                        conversation_id, session_id, user_id, message_id,
                        tool_call_id, action_digest, idempotency_key, status,
                        requested_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        approval_id, kind, summary, _canonical_json(payload), tool_name,
                        _canonical_json(args), conversation_id, session_id, user_id,
                        message_id, tool_call_id, action_digest, idempotency_key,
                        requested_at,
                    ),
                )
                self.conn.execute(
                    """
                    INSERT INTO tool_calls (
                        conversation_id, message_id, approval_id, tool_call_id,
                        tool_name, args_json, action_digest, idempotency_key,
                        status, approved_by, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?)
                    """,
                    (
                        conversation_id, message_id, approval_id, tool_call_id,
                        tool_name, _canonical_json(audit_args),
                        action_digest, idempotency_key, requested_at,
                    ),
                )
                self.conn.commit()
                return approval_id
            except Exception:
                self.conn.rollback()
                raise

    def get_approval(self, approval_id: str) -> Optional[dict]:
        self.expire_stale_approvals()
        row = self._one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return self._approval_row(row)

    def get_execution_receipt(self, approval_id: str) -> Optional[dict]:
        row = self._one(
            "SELECT * FROM execution_receipts WHERE approval_id = ?", (approval_id,)
        )
        return self._receipt_row(row)

    def approval_snapshot(self, approval_id: str) -> Optional[dict]:
        approval = self.get_approval(approval_id)
        if not approval:
            return None
        return {"approval": approval, "receipt": self.get_execution_receipt(approval_id)}

    @staticmethod
    def _validate_binding(
        row: sqlite3.Row,
        *,
        conversation_id: Optional[int],
        session_id: str,
        user_id: str,
    ) -> None:
        if not row["conversation_id"] or not row["session_id"] or not row["user_id"] or not row["message_id"]:
            raise PermissionError("This legacy approval is unbound and cannot execute.")
        if conversation_id is None:
            raise PermissionError("Approval conversation binding is required.")
        if int(row["conversation_id"]) != int(conversation_id):
            raise PermissionError("Approval belongs to a different conversation.")
        if str(row["session_id"]) != str(session_id or ""):
            raise PermissionError("Approval belongs to a different session.")
        if str(row["user_id"]) != str(user_id or ""):
            raise PermissionError("Approval belongs to a different user.")

    def _receipt_locked(self, approval_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM execution_receipts WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        return self._receipt_row(row)

    def _insert_receipt_locked(
        self,
        row: sqlite3.Row,
        *,
        status: str,
        executed: bool,
        success: bool,
        result: Any,
        error: Optional[str],
        completed_at: str,
    ) -> dict:
        existing = self._receipt_locked(str(row["id"]))
        if existing:
            return existing
        receipt_id = secrets.token_urlsafe(16)
        result_json = _canonical_json(result)
        result_hash = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        self.conn.execute(
            """
            INSERT INTO execution_receipts (
                id, approval_id, conversation_id, message_id, tool_call_id,
                tool_name, action_digest, idempotency_key, status, executed,
                success, result_json, result_hash, error, requested_at,
                started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt_id, row["id"], row["conversation_id"], row["message_id"],
                row["tool_call_id"], row["tool_name"], row["action_digest"],
                row["idempotency_key"], status, int(executed), int(success),
                result_json, result_hash, error, row["requested_at"],
                row["started_at"], completed_at,
            ),
        )
        return self._receipt_locked(str(row["id"])) or {}

    def claim_approval(
        self,
        approval_id: str,
        *,
        conversation_id: Optional[int],
        session_id: str,
        user_id: str,
    ) -> dict:
        """Atomically claim pending -> executing. Only the CAS winner runs."""
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT * FROM approvals WHERE id = ?", (approval_id,)
                ).fetchone()
                if not row:
                    raise KeyError("Unknown approval.")
                self._validate_binding(
                    row, conversation_id=conversation_id, session_id=session_id, user_id=user_id
                )
                if row["status"] != "pending":
                    result = {
                        "claimed": False,
                        "replayed": row["status"] in {"succeeded", "failed", "denied", "expired"},
                        "approval": self._approval_row(row),
                        "receipt": self._receipt_locked(approval_id),
                    }
                    self.conn.commit()
                    return result
                started_at = _now_iso()
                changed = self.conn.execute(
                    """
                    UPDATE approvals SET status = 'executing', started_at = ?, decided_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (started_at, started_at, approval_id),
                )
                if changed.rowcount != 1:
                    self.conn.rollback()
                    return self.claim_approval(
                        approval_id, conversation_id=conversation_id,
                        session_id=session_id, user_id=user_id,
                    )
                self.conn.execute(
                    """
                    UPDATE tool_calls SET status = 'executing', approved_by = ?, completed_at = NULL
                    WHERE approval_id = ? AND status = 'pending'
                    """,
                    (user_id, approval_id),
                )
                row = self.conn.execute(
                    "SELECT * FROM approvals WHERE id = ?", (approval_id,)
                ).fetchone()
                self.conn.commit()
                return {
                    "claimed": True, "replayed": False,
                    "approval": self._approval_row(row), "receipt": None,
                }
            except Exception:
                self.conn.rollback()
                raise

    def deny_approval(
        self,
        approval_id: str,
        *,
        conversation_id: Optional[int],
        session_id: str,
        user_id: str,
    ) -> dict:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT * FROM approvals WHERE id = ?", (approval_id,)
                ).fetchone()
                if not row:
                    raise KeyError("Unknown approval.")
                self._validate_binding(
                    row, conversation_id=conversation_id, session_id=session_id, user_id=user_id
                )
                if row["status"] != "pending":
                    result = {
                        "claimed": False,
                        "replayed": row["status"] in {"succeeded", "failed", "denied", "expired"},
                        "approval": self._approval_row(row),
                        "receipt": self._receipt_locked(approval_id),
                    }
                    self.conn.commit()
                    return result
                completed_at = _now_iso()
                changed = self.conn.execute(
                    """
                    UPDATE approvals
                    SET status = 'denied', decided_at = ?, completed_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (completed_at, completed_at, approval_id),
                )
                if changed.rowcount != 1:
                    self.conn.rollback()
                    return self.deny_approval(
                        approval_id, conversation_id=conversation_id,
                        session_id=session_id, user_id=user_id,
                    )
                row = self.conn.execute(
                    "SELECT * FROM approvals WHERE id = ?", (approval_id,)
                ).fetchone()
                result_payload = {
                    "status": "denied", "executed": False,
                    "message": "The operator denied this protected action.",
                }
                receipt = self._insert_receipt_locked(
                    row, status="denied", executed=False, success=False,
                    result=result_payload, error=None, completed_at=completed_at,
                )
                self.conn.execute(
                    """
                    UPDATE tool_calls
                    SET status = 'denied', result_json = ?, result_hash = ?, completed_at = ?
                    WHERE approval_id = ?
                    """,
                    (
                        _canonical_json(result_payload), _sha256_json(result_payload),
                        completed_at, approval_id,
                    ),
                )
                self.conn.commit()
                return {
                    "claimed": True, "replayed": False,
                    "approval": self._approval_row(row), "receipt": receipt,
                }
            except Exception:
                self.conn.rollback()
                raise

    def complete_approval(
        self,
        approval_id: str,
        *,
        success: bool,
        result: Any,
        error: Optional[str] = None,
        executed: bool = True,
    ) -> dict:
        status = "succeeded" if success else "failed"
        completed_at = _now_iso()
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT * FROM approvals WHERE id = ?", (approval_id,)
                ).fetchone()
                if not row:
                    raise KeyError("Unknown approval.")
                if row["status"] != "executing":
                    receipt = self._receipt_locked(approval_id)
                    self.conn.commit()
                    if receipt:
                        return {
                            "claimed": False, "replayed": True,
                            "approval": self._approval_row(row), "receipt": receipt,
                        }
                    raise RuntimeError(f"Approval cannot complete from {row['status']}.")
                changed = self.conn.execute(
                    """
                    UPDATE approvals
                    SET status = ?, completed_at = ?, execution_error = ?
                    WHERE id = ? AND status = 'executing'
                    """,
                    (status, completed_at, error, approval_id),
                )
                if changed.rowcount != 1:
                    raise RuntimeError("Approval execution state changed before completion.")
                row = self.conn.execute(
                    "SELECT * FROM approvals WHERE id = ?", (approval_id,)
                ).fetchone()
                receipt = self._insert_receipt_locked(
                    row, status=status, executed=bool(executed), success=success,
                    result=result, error=error, completed_at=completed_at,
                )
                result_json = _canonical_json(result)
                self.conn.execute(
                    """
                    UPDATE tool_calls
                    SET status = ?, result_json = ?, result_hash = ?, completed_at = ?
                    WHERE approval_id = ?
                    """,
                    (
                        status, result_json,
                        hashlib.sha256(result_json.encode("utf-8")).hexdigest(),
                        completed_at, approval_id,
                    ),
                )
                self.conn.commit()
                return {
                    "claimed": True, "replayed": False,
                    "approval": self._approval_row(row), "receipt": receipt,
                }
            except Exception:
                self.conn.rollback()
                raise

    def recover_interrupted_approvals(self) -> int:
        """Close approvals interrupted by a service restart without rerunning.

        `executed=False` on this receipt means execution was not *verified*;
        the result payload explicitly records that the real-world outcome is
        indeterminate. Failed/success=False keeps every consumer from treating
        the interrupted action as completed while the terminal receipt keeps
        replay exact-once.
        """
        recovered = 0
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self.conn.execute(
                    "SELECT * FROM approvals WHERE status = 'executing'"
                ).fetchall()
                for row in rows:
                    completed_at = _now_iso()
                    error = (
                        "Execution outcome is indeterminate after service restart; "
                        "the protected action was not run again."
                    )
                    changed = self.conn.execute(
                        """
                        UPDATE approvals
                        SET status = 'failed', completed_at = ?, execution_error = ?
                        WHERE id = ? AND status = 'executing'
                        """,
                        (completed_at, error, row["id"]),
                    )
                    if changed.rowcount != 1:
                        continue
                    current = self.conn.execute(
                        "SELECT * FROM approvals WHERE id = ?", (row["id"],)
                    ).fetchone()
                    payload = {
                        "status": "indeterminate",
                        "execution_state": "indeterminate",
                        "executed": False,
                        "success": False,
                        "may_have_executed": True,
                        "message": error,
                    }
                    self._insert_receipt_locked(
                        current, status="failed", executed=False, success=False,
                        result=payload, error=error, completed_at=completed_at,
                    )
                    self.conn.execute(
                        """
                        UPDATE tool_calls
                        SET status = 'failed', result_json = ?, result_hash = ?, completed_at = ?
                        WHERE approval_id = ?
                        """,
                        (
                            _canonical_json(payload), _sha256_json(payload),
                            completed_at, row["id"],
                        ),
                    )
                    recovered += 1
                self.conn.commit()
                return recovered
            except Exception:
                self.conn.rollback()
                raise

    def expire_stale_approvals(self, ttl: timedelta = APPROVAL_TTL) -> int:
        cutoff = _utcnow() - ttl
        expired = 0
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM approvals WHERE status = 'pending'"
            ).fetchall()
            for row in rows:
                try:
                    requested = datetime.fromisoformat(str(row["requested_at"]).replace("Z", "+00:00"))
                    if requested.tzinfo is None:
                        requested = requested.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if requested > cutoff:
                    continue
                completed_at = _now_iso()
                changed = self.conn.execute(
                    """
                    UPDATE approvals SET status = 'expired', completed_at = ?, decided_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (completed_at, completed_at, row["id"]),
                )
                if changed.rowcount != 1:
                    continue
                current = self.conn.execute(
                    "SELECT * FROM approvals WHERE id = ?", (row["id"],)
                ).fetchone()
                payload = {
                    "status": "expired", "executed": False,
                    "message": "The protected action expired before approval.",
                }
                self._insert_receipt_locked(
                    current, status="expired", executed=False, success=False,
                    result=payload, error=None, completed_at=completed_at,
                )
                self.conn.execute(
                    """
                    UPDATE tool_calls
                    SET status = 'expired', result_json = ?, result_hash = ?, completed_at = ?
                    WHERE approval_id = ?
                    """,
                    (_canonical_json(payload), _sha256_json(payload), completed_at, row["id"]),
                )
                expired += 1
            if expired:
                self.conn.commit()
        return expired

    # ---------- google tokens ----------

    def save_google_token(self, token: dict) -> None:
        self._exec(
            """
            INSERT INTO google_tokens (id, access_token, refresh_token, scope, expires_at, account_email, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                access_token  = excluded.access_token,
                -- Google only returns a refresh_token on first consent; never
                -- let a later refresh response blank out the one we hold.
                refresh_token = COALESCE(excluded.refresh_token, google_tokens.refresh_token),
                scope         = excluded.scope,
                expires_at    = excluded.expires_at,
                account_email = COALESCE(excluded.account_email, google_tokens.account_email),
                updated_at    = excluded.updated_at
            """,
            (
                token.get("access_token"), token.get("refresh_token"), token.get("scope"),
                token.get("expires_at"), token.get("account_email"),
            ),
        )

    def get_google_token(self) -> Optional[dict]:
        row = self._one("SELECT * FROM google_tokens WHERE id = 1")
        return dict(row) if row else None

    def clear_google_token(self) -> None:
        self._exec("DELETE FROM google_tokens WHERE id = 1")

    # ---------- generic records ----------

    def get_record(self, namespace: str, record_id: str) -> Optional[dict]:
        row = self._one(
            "SELECT payload_json FROM state_records WHERE namespace = ? AND id = ?",
            (namespace, record_id),
        )
        return json.loads(row["payload_json"]) if row else None

    def put_record(self, namespace: str, record_id: str, payload: dict) -> dict:
        self._exec(
            """
            INSERT INTO state_records (namespace, id, payload_json, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(namespace, id) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at   = excluded.updated_at
            """,
            (namespace, record_id, json.dumps(payload)),
        )
        return payload

    # ---------- tasks ----------

    def add_task(self, title: str, conversation_id: Optional[int] = None,
                 due_at: Optional[str] = None) -> int:
        return self._exec(
            "INSERT INTO tasks (conversation_id, title, due_at) VALUES (?, ?, ?)",
            (conversation_id, title, due_at),
        ).lastrowid

    def list_tasks(self, status: Optional[str] = None) -> list[dict]:
        if status:
            rows = self._query(
                "SELECT * FROM tasks WHERE status = ? ORDER BY COALESCE(due_at,'9999'), id", (status,)
            )
        else:
            rows = self._query("SELECT * FROM tasks ORDER BY COALESCE(due_at,'9999'), id")
        return [dict(r) for r in rows]

    def set_task_status(self, task_id: int, status: str) -> None:
        self._exec(
            "UPDATE tasks SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, task_id),
        )

    # ---------- audit ----------

    def audit(self, event_type: str, detail: Optional[dict] = None) -> None:
        self._exec(
            "INSERT INTO audit_log (event_type, detail_json) VALUES (?, ?)",
            (event_type, json.dumps(detail, default=str) if detail is not None else None),
        )

    # ---------- worker state ----------

    def upsert_worker_state(self, name: str, port: int, pid: Optional[int], status: str) -> None:
        self._exec(
            """
            INSERT INTO worker_state (worker_name, port, pid, status, last_swap_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(worker_name) DO UPDATE SET
                port = excluded.port, pid = excluded.pid,
                status = excluded.status, last_swap_at = excluded.last_swap_at
            """,
            (name, port, pid, status),
        )

    def close(self) -> None:
        with self._lock:
            self.conn.close()
