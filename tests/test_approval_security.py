from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.routes import create_router
from core.state.db import Store
from core.orchestrator.loop import Orchestrator
from core.tools.builtin.system import MAX_READ_BYTES, make_list_directory, make_read_file
from core.tools.registry import Registry, ToolBlocked, ToolError


def _policy(tmp_path: Path, *, reference_root: Path | None = None) -> Path:
    allowed = tmp_path / "allowed"
    allowed.mkdir(exist_ok=True)
    roots = [allowed]
    if reference_root:
        reference_root.mkdir(exist_ok=True)
        roots.append(reference_root)
    policy = tmp_path / "tools.yaml"
    root_lines = "\n".join(f'  - "{str(root).replace(chr(92), chr(92) * 2)}"' for root in roots)
    write_line = str(allowed).replace("\\", "\\\\")
    policy.write_text(
        f"""
roots:
{root_lines}
write_roots:
  - "{write_line}"
tools:
  write_file:
    tier: confirm_required
  read_file:
    tier: read_only
  list_directory:
    tier: read_only
  run_powershell:
    tier: confirm_required
""".strip(),
        encoding="utf-8",
    )
    return policy


def _bound_approval(store: Store, *, args: dict | None = None) -> tuple[int, int, str]:
    conversation_id = store.create_conversation("approval test")
    message_id = store.add_message(conversation_id, "user", "perform the protected action")
    approval_id = store.create_approval(
        "write_file",
        "Write a test file",
        {"name": "write_file", "args": args or {"path": "safe.txt", "content": "ok"}},
        conversation_id=conversation_id,
        session_id="session-a",
        user_id="owner-a",
        message_id=message_id,
        tool_call_id="call-a",
        logged_args={
            "path": "safe.txt",
            "content": {"redacted": True, "bytes": 2, "sha256": "test-digest"},
        },
    )
    return conversation_id, message_id, approval_id


def _bound_powershell_approval(
    store: Store, *, command: str = "Write-Output test"
) -> tuple[int, int, str]:
    conversation_id = store.create_conversation("PowerShell approval test")
    message_id = store.add_message(conversation_id, "user", "run the protected command")
    approval_id = store.create_approval(
        "run_powershell",
        f"Run PowerShell: {command}",
        {"name": "run_powershell", "args": {"command": command}},
        conversation_id=conversation_id,
        session_id="session-a",
        user_id="owner-a",
        message_id=message_id,
        tool_call_id="powershell-call-a",
    )
    return conversation_id, message_id, approval_id


def test_exact_once_concurrent_claim_and_receipt_replay(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite")
    registry = Registry(_policy(tmp_path), store=store)
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def protected(args: dict) -> dict:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"ok": True, "path": args["path"]}

    registry.register("write_file", protected)
    conversation_id, message_id, approval_id = _bound_approval(store)

    async def scenario() -> tuple[dict, dict, dict]:
        first = asyncio.create_task(registry.resolve_approval(
            approval_id, True, conversation_id=conversation_id,
            session_id="session-a", user_id="owner-a",
        ))
        await entered.wait()
        concurrent = await registry.resolve_approval(
            approval_id, True, conversation_id=conversation_id,
            session_id="session-a", user_id="owner-a",
        )
        release.set()
        completed = await first
        replay = await registry.resolve_approval(
            approval_id, True, conversation_id=conversation_id,
            session_id="session-a", user_id="owner-a",
        )
        return concurrent, completed, replay

    concurrent, completed, replay = asyncio.run(scenario())

    assert calls == 1
    assert concurrent["claimed"] is False
    assert concurrent["approval"]["status"] == "executing"
    assert concurrent["receipt"] is None
    assert completed["approval"]["status"] == "succeeded"
    assert completed["receipt"]["executed"] is True
    assert completed["receipt"]["success"] is True
    assert replay["replayed"] is True
    assert replay["receipt"]["receipt_id"] == completed["receipt"]["receipt_id"]
    assert replay["receipt"]["result_hash"] == completed["receipt"]["result_hash"]

    tool_calls = store.conn.execute(
        "SELECT * FROM tool_calls WHERE approval_id = ?", (approval_id,)
    ).fetchall()
    assert len(tool_calls) == 1
    assert tool_calls[0]["conversation_id"] == conversation_id
    assert tool_calls[0]["message_id"] == message_id
    assert tool_calls[0]["status"] == "succeeded"
    logged_args = json.loads(tool_calls[0]["args_json"])
    assert logged_args["content"]["redacted"] is True
    store.close()


def test_denied_replay_and_later_approve_never_execute(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite")
    registry = Registry(_policy(tmp_path), store=store)
    calls = 0

    def protected(_args: dict) -> dict:
        nonlocal calls
        calls += 1
        return {"ok": True}

    registry.register("write_file", protected)
    conversation_id, _message_id, approval_id = _bound_approval(store)

    async def scenario() -> tuple[dict, dict, dict]:
        denied = await registry.resolve_approval(
            approval_id, False, conversation_id=conversation_id,
            session_id="session-a", user_id="owner-a",
        )
        denied_replay = await registry.resolve_approval(
            approval_id, False, conversation_id=conversation_id,
            session_id="session-a", user_id="owner-a",
        )
        approve_replay = await registry.resolve_approval(
            approval_id, True, conversation_id=conversation_id,
            session_id="session-a", user_id="owner-a",
        )
        return denied, denied_replay, approve_replay

    denied, denied_replay, approve_replay = asyncio.run(scenario())
    assert calls == 0
    assert denied["receipt"]["status"] == "denied"
    assert denied["receipt"]["executed"] is False
    assert denied["receipt"]["success"] is False
    assert denied_replay["receipt"]["receipt_id"] == denied["receipt"]["receipt_id"]
    assert approve_replay["approval"]["status"] == "denied"
    assert approve_replay["receipt"]["receipt_id"] == denied["receipt"]["receipt_id"]
    store.close()


@pytest.mark.parametrize(
    ("result", "expected_error"),
    [
        (
            {"exit_code": 17, "timed_out": False, "stdout": "", "stderr": "failed"},
            "PowerShell exited with code 17.",
        ),
        (
            {"exit_code": -1, "timed_out": True, "stdout": "", "stderr": ""},
            "PowerShell command timed out.",
        ),
    ],
)
def test_powershell_process_failure_is_terminal_and_replays_without_rerun(
    tmp_path: Path, result: dict, expected_error: str,
) -> None:
    store = Store(tmp_path / "state.sqlite")
    registry = Registry(_policy(tmp_path), store=store)
    calls = 0

    def protected(_args: dict) -> dict:
        nonlocal calls
        calls += 1
        return result

    registry.register("run_powershell", protected)
    conversation_id, _message_id, approval_id = _bound_powershell_approval(store)

    completed = asyncio.run(registry.resolve_approval(
        approval_id, True, conversation_id=conversation_id,
        session_id="session-a", user_id="owner-a",
    ))
    replay = asyncio.run(registry.resolve_approval(
        approval_id, True, conversation_id=conversation_id,
        session_id="session-a", user_id="owner-a",
    ))

    assert calls == 1
    assert completed["approval"]["status"] == "failed"
    assert completed["receipt"]["status"] == "failed"
    assert completed["receipt"]["executed"] is True
    assert completed["receipt"]["success"] is False
    assert completed["receipt"]["error"] == expected_error
    assert completed["receipt"]["result"] == result
    assert replay["replayed"] is True
    assert replay["receipt"]["receipt_id"] == completed["receipt"]["receipt_id"]
    assert replay["receipt"]["result_hash"] == completed["receipt"]["result_hash"]
    tool_call = store.conn.execute(
        "SELECT status FROM tool_calls WHERE approval_id = ?", (approval_id,)
    ).fetchone()
    assert tool_call["status"] == "failed"
    store.close()


def test_restart_closes_executing_approval_as_indeterminate_and_never_reruns(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.sqlite"
    first_store = Store(db_path)
    conversation_id, _message_id, approval_id = _bound_powershell_approval(first_store)
    claimed = first_store.claim_approval(
        approval_id, conversation_id=conversation_id,
        session_id="session-a", user_id="owner-a",
    )
    assert claimed["claimed"] is True
    assert claimed["approval"]["status"] == "executing"
    first_store.close()

    restarted_store = Store(db_path)
    recovered = restarted_store.approval_snapshot(approval_id)
    assert recovered is not None
    assert recovered["approval"]["status"] == "failed"
    assert recovered["receipt"]["status"] == "failed"
    assert recovered["receipt"]["executed"] is False
    assert recovered["receipt"]["success"] is False
    assert recovered["receipt"]["result"]["execution_state"] == "indeterminate"
    assert recovered["receipt"]["result"]["may_have_executed"] is True
    assert recovered["receipt"]["execution_state"] == "indeterminate"
    assert recovered["receipt"]["may_have_executed"] is True
    assert "indeterminate" in recovered["receipt"]["outcome_message"]
    receipt_id = recovered["receipt"]["receipt_id"]

    calls = 0

    def protected(_args: dict) -> dict:
        nonlocal calls
        calls += 1
        return {"exit_code": 0, "timed_out": False}

    registry = Registry(_policy(tmp_path), store=restarted_store)
    registry.register("run_powershell", protected)
    projected = registry.public_approval(
        recovered["approval"], receipt=recovered["receipt"]
    )
    assert projected["execution_state"] == "indeterminate"
    assert projected["may_have_executed"] is True
    assert "indeterminate" in projected["outcome_message"]
    replay = asyncio.run(registry.resolve_approval(
        approval_id, True, conversation_id=conversation_id,
        session_id="session-a", user_id="owner-a",
    ))
    assert calls == 0
    assert replay["replayed"] is True
    assert replay["receipt"]["receipt_id"] == receipt_id
    restarted_store.close()

    reopened_store = Store(db_path)
    assert reopened_store.get_execution_receipt(approval_id)["receipt_id"] == receipt_id
    reopened_store.close()


def test_approval_request_and_terminal_receipt_are_persisted_as_message_artifacts(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.sqlite")
    registry = Registry(_policy(tmp_path), store=store)
    secret = "approval-display-secret"
    content = f'password="{secret}"\nordinary body'
    executed_args = []

    def write_file(args: dict) -> dict:
        executed_args.append(dict(args))
        return {"ok": True, "bytes": len(str(args["content"]).encode("utf-8"))}

    registry.register("write_file", write_file)

    class Router:
        active_name = "omni"

        @staticmethod
        def active_config():
            return SimpleNamespace(supports_vision=True, supports_audio=True)

    class Client:
        mode = "approval"

        async def stream(self, _messages, tools=None):
            if self.mode == "approval":
                yield {
                    "type": "tool_call", "id": "artifact-call",
                    "name": "write_file",
                    "arguments": json.dumps({"path": "safe.txt", "content": content}),
                }
            else:
                yield {"type": "content", "text": "The receipt confirms the write."}

    client = Client()
    orchestrator = Orchestrator(
        Router(), client, registry, store,
        SimpleNamespace(context_tokens=32768, max_response_tokens=1024),
    )
    conversation_id = store.create_conversation("artifacts")
    user_message_id = store.add_message(conversation_id, "user", "write it")

    async def collect(**kwargs) -> list[dict]:
        return [event async for event in orchestrator.run_turn(conversation_id, **kwargs)]

    staged_events = asyncio.run(collect(
        user_message="write it",
        approval_context={
            "session_id": "local:local-dev", "user_id": "local-dev",
            "message_id": user_message_id,
        },
    ))
    request = next(event["approval"] for event in staged_events if event["type"] == "approval")
    assert secret not in json.dumps(request)
    assert request["args"]["content"] == {
        "redacted": True,
        "bytes": len(content.encode("utf-8")),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }
    staged_message = store.get_messages(conversation_id)[-1]
    assert staged_message["artifacts"] == [{"type": "approval_request", "data": request}]
    assert secret not in json.dumps(staged_message["artifacts"])

    raw_record = store.get_approval(request["id"])
    assert raw_record["args"] == {"path": "safe.txt", "content": content}
    assert raw_record["action_digest"] == request["action_digest"]

    async def local_session():
        return {"google_sub": "local-dev"}

    app = FastAPI()
    app.include_router(create_router(
        SimpleNamespace(), store, Router(), registry, local_session,
    ))
    rest_response = TestClient(app).get(f"/api/approvals/{request['id']}")
    assert rest_response.status_code == 200
    assert secret not in rest_response.text
    assert rest_response.json()["approval"]["args"] == request["args"]
    assert rest_response.json()["approval"]["summary"] == request["summary"]

    outcome = asyncio.run(registry.resolve_approval(
        request["id"], True, conversation_id=conversation_id,
        session_id="local:local-dev", user_id="local-dev",
    ))
    assert executed_args == [{"path": "safe.txt", "content": content}]
    client.mode = "continuation"
    receipt = outcome["receipt"]
    resumed_events = asyncio.run(collect(
        user_message="",
        approved_tool={
            "name": "write_file", "args": raw_record["args"],
            "result": receipt["result"], "receipt": receipt, "call_id": "artifact-call",
        },
        approval_context={
            "session_id": "local:local-dev", "user_id": "local-dev",
            "message_id": user_message_id,
        },
    ))
    assert any(
        event.get("artifact", {}).get("type") == "execution_receipt"
        for event in resumed_events
    )
    completed_message = store.get_messages(conversation_id)[-1]
    receipt_artifact = next(
        artifact for artifact in completed_message["artifacts"]
        if artifact["type"] == "execution_receipt"
    )
    assert receipt_artifact["data"]["approval_id"] == request["id"]
    assert receipt_artifact["data"]["status"] == "succeeded"
    assert receipt_artifact["data"]["executed"] is True
    assert receipt_artifact["data"]["success"] is True
    store.close()


def test_public_approval_summary_and_args_redact_recognized_command_secret(
    tmp_path: Path,
) -> None:
    registry = Registry(_policy(tmp_path))
    secret = "command-display-secret"
    command = f'Write-Output password="{secret}"'
    projected = registry.public_approval({
        "id": "approval-id",
        "tool_name": "run_powershell",
        "summary": f"Run PowerShell: {command}",
        "args": {"command": command},
        "session_id": "session-secret",
        "user_id": "owner-secret",
        "payload": {"name": "run_powershell", "args": {"command": command}},
        "status": "pending",
    })
    rendered = json.dumps(projected)
    assert secret not in rendered
    assert "[REDACTED]" in projected["args"]["command"]
    assert "[REDACTED]" in projected["summary"]
    assert "session_id" not in projected
    assert "user_id" not in projected
    assert "payload" not in projected


def test_approval_binding_fails_closed_for_conversation_session_user_and_message(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "state.sqlite")
    registry = Registry(_policy(tmp_path), store=store)
    registry.register("write_file", lambda _args: {"ok": True})
    conversation_id, message_id, approval_id = _bound_approval(store)
    other_conversation = store.create_conversation("other")

    async def reject(**overrides) -> None:
        kwargs = {
            "conversation_id": conversation_id,
            "session_id": "session-a",
            "user_id": "owner-a",
            **overrides,
        }
        with pytest.raises(PermissionError):
            await registry.resolve_approval(approval_id, True, **kwargs)

    asyncio.run(reject(conversation_id=other_conversation))
    asyncio.run(reject(conversation_id=None))
    asyncio.run(reject(session_id="session-b"))
    asyncio.run(reject(user_id="owner-b"))
    assert store.get_approval(approval_id)["status"] == "pending"

    wrong_message = store.add_message(other_conversation, "user", "wrong")
    with pytest.raises(ValueError, match="does not belong"):
        store.create_approval(
            "write_file", "bad binding",
            {"name": "write_file", "args": {"path": "bad.txt", "content": "bad"}},
            conversation_id=conversation_id, session_id="session-a", user_id="owner-a",
            message_id=wrong_message, tool_call_id="call-b",
        )

    completed = asyncio.run(registry.resolve_approval(
        approval_id, True, conversation_id=conversation_id,
        session_id="session-a", user_id="owner-a",
    ))
    assert completed["approval"]["message_id"] == message_id
    assert completed["approval"]["status"] == "succeeded"
    store.close()


def test_auth_disabled_local_identity_is_still_session_and_user_bound(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite")
    registry = Registry(_policy(tmp_path), store=store)
    registry.register("write_file", lambda _args: {"ok": True})
    conversation_id = store.create_conversation("local setup")
    message_id = store.add_message(conversation_id, "user", "write locally")
    approval_id = store.create_approval(
        "write_file", "local protected write",
        {"name": "write_file", "args": {"path": "local.txt", "content": "ok"}},
        conversation_id=conversation_id,
        session_id="local:local-dev",
        user_id="local-dev",
        message_id=message_id,
        tool_call_id="local-call",
    )

    with pytest.raises(PermissionError, match="different session"):
        asyncio.run(registry.resolve_approval(
            approval_id, True, conversation_id=conversation_id,
            session_id="local:other", user_id="local-dev",
        ))
    completed = asyncio.run(registry.resolve_approval(
        approval_id, True, conversation_id=conversation_id,
        session_id="local:local-dev", user_id="local-dev",
    ))
    assert completed["approval"]["status"] == "succeeded"
    store.close()


def test_session_cookie_is_hashed_and_legacy_raw_sessions_expire(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sessions (
            token TEXT PRIMARY KEY, google_sub TEXT NOT NULL, user_agent TEXT,
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        );
        INSERT INTO sessions VALUES (
            'legacy-raw-cookie', 'owner', 'browser',
            '2026-01-01 00:00:00', '2099-01-01T00:00:00+00:00'
        );
    """)
    conn.commit()
    conn.close()

    store = Store(path)
    assert store.get_session("legacy-raw-cookie") is None
    token = store.create_session("owner", "browser", 1)
    session = store.get_session(token)
    assert session is not None
    assert session["token_hash"] != token
    assert "token" not in session
    other_token = store.create_session("owner", "second browser", 1)
    other_session = store.get_session(other_token)
    assert other_session is not None
    first_identity = f"session:{session['token_hash']}"
    second_identity = f"session:{other_session['token_hash']}"
    assert first_identity != second_identity
    columns = {row["name"] for row in store.conn.execute("PRAGMA table_info(sessions)")}
    assert columns == {
        "token_hash", "user_id", "google_sub", "tailscale_login",
        "user_agent", "created_at", "expires_at",
    }
    store.close()

    for candidate in tmp_path.glob("sessions.sqlite*"):
        assert token.encode() not in candidate.read_bytes()
        assert b"legacy-raw-cookie" not in candidate.read_bytes()


def test_get_messages_returns_newest_window_in_chronological_order(tmp_path: Path) -> None:
    store = Store(tmp_path / "state.sqlite")
    conversation_id = store.create_conversation("long chat")
    ids = [
        store.add_message(conversation_id, "user", f"message-{index}")
        for index in range(520)
    ]
    messages = store.get_messages(conversation_id, limit=500)
    assert len(messages) == 500
    assert messages[0]["id"] == ids[20]
    assert messages[0]["content"] == "message-20"
    assert messages[-1]["id"] == ids[-1]
    assert messages[-1]["content"] == "message-519"
    store.close()


def test_secret_paths_are_denied_and_safe_results_are_bounded_and_redacted(
    tmp_path: Path,
) -> None:
    reference = tmp_path / "reference"
    policy = _policy(tmp_path, reference_root=reference)
    store = Store(tmp_path / "state.sqlite")
    registry = Registry(policy, store=store)
    registry.register("read_file", make_read_file(registry))
    registry.register("list_directory", make_list_directory(registry))

    allowed = tmp_path / "allowed"
    (allowed / "config").mkdir()
    (allowed / "config" / ".env.local").write_text("CLIENT_SECRET=do-not-read", encoding="utf-8")
    (allowed / "credentials").mkdir()
    (allowed / "credentials" / "google.json").write_text("{}", encoding="utf-8")
    (allowed / "copy.sqlite-wal").write_bytes(b"private db material")
    (allowed / "operator-private.pem").write_text("PRIVATE KEY", encoding="utf-8")
    safe = allowed / "safe.txt"
    safe.write_text(
        "ACCESS_TOKEN=top-secret\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz\n"
        + "x" * (MAX_READ_BYTES + 500),
        encoding="utf-8",
    )

    async def scenario() -> tuple[dict, dict]:
        for path in (
            allowed / "config" / ".env.local",
            allowed / "credentials" / "google.json",
            allowed / "copy.sqlite-wal",
            allowed / "operator-private.pem",
            store.db_path,
        ):
            with pytest.raises(ToolBlocked):
                await registry.invoke("read_file", {"path": str(path)})
        result = await registry.invoke("read_file", {"path": str(safe)})
        listing = await registry.invoke("list_directory", {"path": str(allowed)})
        return result, listing

    result, listing = asyncio.run(scenario())
    assert result["returned_bytes"] == MAX_READ_BYTES
    assert result["truncated"] is True
    assert "top-secret" not in result["content"]
    assert "Bearer abcdefghijklmnopqrstuvwxyz" not in result["content"]
    assert "[REDACTED]" in result["content"]
    assert listing["filtered_protected_entries"] >= 2
    assert not {"copy.sqlite-wal", "credentials"} & {
        entry["name"] for entry in listing["entries"]
    }
    with pytest.raises(ToolError):
        registry.check_path(reference / "must-stay-read-only.txt", write=True)
    logged = store.conn.execute(
        "SELECT result_json FROM tool_calls WHERE tool_name = 'read_file' ORDER BY id DESC LIMIT 1"
    ).fetchone()["result_json"]
    assert "top-secret" not in logged
    assert json.loads(logged)["content"]["redacted"] is True
    store.close()


def test_existing_legacy_pending_approval_migrates_to_non_executable_expired(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE conversations (id INTEGER PRIMARY KEY, title TEXT);
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, conversation_id INTEGER, role TEXT, content TEXT
        );
        CREATE TABLE tool_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER,
            tool_name TEXT NOT NULL, args_json TEXT NOT NULL, result_json TEXT,
            approved_by TEXT, created_at TEXT
        );
        CREATE TABLE approvals (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, summary TEXT NOT NULL,
            payload_json TEXT NOT NULL, status TEXT NOT NULL,
            requested_at TEXT, decided_at TEXT
        );
        INSERT INTO approvals VALUES (
            'legacy-approval', 'write_file', 'legacy write',
            '{"name":"write_file","args":{"path":"old.txt"}}',
            'pending', '2026-01-01 00:00:00', NULL
        );
    """)
    conn.commit()
    conn.close()

    store = Store(path)
    migrated = store.get_approval("legacy-approval")
    assert migrated is not None
    assert migrated["status"] == "expired"
    with pytest.raises(PermissionError, match="unbound"):
        store.claim_approval(
            "legacy-approval", conversation_id=None,
            session_id="session-a", user_id="owner-a",
        )
    store.close()
