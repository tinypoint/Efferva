from uuid import uuid4

from efferva.events import (
    raw,
    run_error,
    run_finished,
    run_started,
    text_message_content,
    text_message_end,
    text_message_start,
)


def test_ag_ui_event_shapes() -> None:
    thread_id = uuid4()

    assert run_started(thread_id, "run-1", {"prompt": "hello"})["type"] == "RUN_STARTED"
    assert run_finished(thread_id, "run-1")["threadId"] == str(thread_id)
    assert run_error("boom")["code"] == "RUNTIME_ERROR"
    assert text_message_start("message-1")["role"] == "assistant"
    assert text_message_content("message-1", "hello")["delta"] == "hello"
    assert text_message_end("message-1")["type"] == "TEXT_MESSAGE_END"
    assert raw({"method": "turn/plan/updated"})["source"] == "codex-app-server"
