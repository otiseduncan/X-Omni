from types import SimpleNamespace

import pytest

from core.orchestrator import loop as loop_mod
from core.orchestrator.loop import Orchestrator, video_failure_summary


class _Store:
    def __init__(self) -> None:
        self.persisted = False
        self.saved = None

    def get_messages(self, _conversation_id):
        return []

    def add_message(self, *args, **kwargs):
        self.persisted = True
        self.saved = (args, kwargs)
        return 91

    def touch_conversation(self, *_args, **_kwargs):
        return None


class _NoModelClient:
    async def stream(self, *_args, **_kwargs):
        raise AssertionError("terminal video failure must not call the model")
        yield  # pragma: no cover


def _failure_result(*, submit_state: str, may_have_generated: bool) -> dict:
    return {
        "ok": False,
        "status": "failed",
        "executed": True,
        "success": False,
        "actual_video": False,
        "actual_generation": False,
        "verified": False,
        "stage": "model_stop_readiness",
        "retryable": True,
        "message": "Video generation did not complete.",
        "lifecycle": {
            "mode": "sequential_exclusive",
            "previous_worker": "omni",
            "model_stop_attempted": True,
            "model_stopped": False,
            "model_restore_required": False,
            "model_restored": True,
            "gpu_indices": [0, 1],
            "external_runtime": "not_started",
            "runtime_release_attempted": False,
            "runtime_released": True,
            "request_files_cleanup_attempted": False,
            "request_files_cleaned": True,
        },
        "generation": {
            "submit_state": submit_state,
            "prompt_id_known": False,
            "prompt_delete_requested": False,
            "prompt_cancelled": None,
            "may_have_generated": may_have_generated,
            "may_have_surviving_output": False,
            "output_removed": None,
        },
    }


@pytest.mark.asyncio
async def test_failed_video_is_persisted_before_emit_and_never_calls_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _failure_result(
        submit_state="not_attempted", may_have_generated=False
    )
    receipt = {
        "id": "receipt-timeout",
        "approval_id": "approval-timeout",
        "tool_name": "video_generate",
        "status": "failed",
        "executed": True,
        "success": False,
        "result": result,
    }
    store = _Store()
    monkeypatch.setattr(loop_mod.prompt_mod, "build_messages", lambda *_a, **_k: [])
    orchestrator = Orchestrator(
        SimpleNamespace(active_name="omni"),
        _NoModelClient(),
        SimpleNamespace(model_tools=lambda: []),
        store,
        SimpleNamespace(context_tokens=32000, max_response_tokens=1000),
    )
    approved = {
        "name": "video_generate",
        "args": {"mode": "image_to_video", "source_sha256": "a" * 64},
        "result": result,
        "receipt": receipt,
        "call_id": "video-failure-1",
    }

    stream = orchestrator._run(1, "", approved, None)
    first = await anext(stream)
    assert store.persisted is True
    events = [first, *[event async for event in stream]]

    summary = (
        "Video generation did not start because Omni's readiness check could not "
        "be completed. No video job was submitted, and Omni was not stopped."
    )
    assert [event for event in events if event.get("type") == "token"] == [
        {"type": "token", "text": summary}
    ]
    assert events[-1]["type"] == "done"
    assert all(event.get("type") != "error" for event in events)
    saved_args, saved_kwargs = store.saved
    assert saved_args[2] == summary
    assert [artifact["type"] for artifact in saved_kwargs["artifacts"]] == [
        "execution_receipt",
        "video_generation_status",
    ]


def test_video_failure_summary_preserves_indeterminate_submission_truth() -> None:
    result = _failure_result(submit_state="indeterminate", may_have_generated=True)
    assert video_failure_summary(result) == (
        "Video generation may have begun, but no verified playable result is being "
        "claimed. The receipt records the cleanup and restoration outcome."
    )


def test_video_failure_summary_does_not_hide_verified_partial_output() -> None:
    result = _failure_result(submit_state="accepted", may_have_generated=True)
    result["actual_video"] = True
    result["actual_generation"] = True
    result["verified"] = True
    assert video_failure_summary(result) == (
        "A video file was generated, but final lifecycle verification failed. "
        "The receipt preserves that partial result; no playable success card is "
        "being claimed."
    )
