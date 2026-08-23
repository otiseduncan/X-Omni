from pathlib import Path


def test_chat_websocket_can_receive_stop_while_turn_runs():
    source = Path("core/api/chat.py").read_text(encoding="utf-8")
    assert 'if kind == "stop":' in source
    assert "asyncio.create_task(coro)" in source
    assert "task.cancel()" in source
    assert '"type": "cancelled"' in source
    assert '"partial_saved"' in source
    assert '"workload": "response"' in source


def test_cancelled_partial_response_is_persisted_before_reconcile():
    source = Path("core/api/chat.py").read_text(encoding="utf-8")
    assert "clean_partial = partial.strip()" in source
    assert 'store.add_message(' in source
    assert '"assistant"' in source
    assert "worker_used=router_.active_name" in source


def test_client_protocol_documents_stop_frame():
    source = Path("core/api/chat.py").read_text(encoding="utf-8")
    assert '{"type":"stop","conversation_id":N|null}' in source
