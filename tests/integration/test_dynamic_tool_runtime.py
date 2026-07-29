from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from efferva import Tool, ToolContext
from efferva.runtime import CodexRuntime
from efferva.sandbox import SandboxEnvironment, SandboxHandle


def _event(payload: dict[str, Any]) -> str:
    return f"event: {payload['type']}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


def _completed(response_id: str) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "usage": {
                "input_tokens": 0,
                "input_tokens_details": None,
                "output_tokens": 0,
                "output_tokens_details": None,
                "total_tokens": 0,
            },
        },
    }


class _DynamicToolResponsesHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        inputs = body.get("input") or []
        has_tool_output = any(item.get("type") == "function_call_output" for item in inputs)
        if has_tool_output:
            events = [
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": "message-1",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "The application tool returned 42.",
                            }
                        ],
                    },
                },
                _completed("response-2"),
            ]
        else:
            events = [
                {"type": "response.created", "response": {"id": "response-1"}},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "add-call",
                        "name": "add_numbers",
                        "arguments": json.dumps({"left": 20, "right": 22}),
                    },
                },
                _completed("response-1"),
            ]

        payload = "".join(_event(event) for event in events).encode()
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(payload)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _runtime_configuration() -> tuple[str | None, Path]:
    return (
        os.getenv("EFFERVA_TEST_DATABASE_URL"),
        Path(
            os.getenv(
                "EFFERVA_TEST_RUNTIME_BINARY",
                "target/debug/efferva-codex-runtime",
            )
        ).resolve(),
    )


@pytest.mark.integration
async def test_dynamic_tool_round_trip_through_codex_app_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url, binary = _runtime_configuration()
    if not database_url:
        pytest.skip("EFFERVA_TEST_DATABASE_URL is not set")
    if not binary.exists():
        pytest.skip(f"runtime binary not found: {binary}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), _DynamicToolResponsesHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    monkeypatch.setenv("OPENAI_API_KEY", "mock")
    invocations: list[dict[str, Any]] = []

    async def add_numbers(
        _: ToolContext,
        arguments: dict[str, Any],
    ) -> dict[str, int]:
        invocations.append(dict(arguments))
        return {"total": int(arguments["left"]) + int(arguments["right"])}

    runtime = CodexRuntime(
        binary,
        database_url,
        openai_base_url=f"http://127.0.0.1:{server.server_port}/v1",
        model="mock",
        tools=[
            Tool(
                name="add_numbers",
                description="Add two integers.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "left": {"type": "integer"},
                        "right": {"type": "integer"},
                    },
                    "required": ["left", "right"],
                    "additionalProperties": False,
                },
                handler=add_numbers,
            )
        ],
    )
    sandbox = SandboxEnvironment(
        environment_id="local",
        endpoint="",
        workspace_path=str(tmp_path),
        sandbox=SandboxHandle(
            provider="test",
            external_ref="local",
            workspace_id=uuid4(),
        ),
    )

    try:
        await runtime.start()
        thread_id = await runtime.start_thread(sandbox)
        queue = runtime.subscribe(thread_id)
        turn_id = await runtime.start_turn(
            thread_id,
            "Use add_numbers to add 20 and 22.",
            sandbox,
        )
        async with asyncio.timeout(30):
            while True:
                notification = await queue.get()
                if (
                    notification.get("method") == "turn/completed"
                    and (notification.get("params") or {}).get("turn", {}).get("id") == turn_id
                ):
                    break
        assert invocations == [{"left": 20, "right": 22}]
    finally:
        await runtime.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
