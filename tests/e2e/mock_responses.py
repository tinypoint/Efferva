from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


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


class ResponsesHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        inputs = body.get("input") or []
        has_tool_output = any(
            item.get("type") == "function_call_output" and item.get("call_id") == "sandbox-proof"
            for item in inputs
        )

        if has_tool_output:
            events = [
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": "message-1",
                        "content": [{"type": "output_text", "text": "sandbox-ok"}],
                    },
                },
                _completed("response-2"),
            ]
        else:
            events = [
                {
                    "type": "response.created",
                    "response": {"id": "response-1"},
                },
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "sandbox-proof",
                        "name": "exec_command",
                        "arguments": json.dumps(
                            {
                                "cmd": (
                                    "printf sandbox-ok > /workspace/proof.txt "
                                    "&& cat /workspace/proof.txt"
                                ),
                                "login": False,
                            }
                        ),
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


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 18090), ResponsesHandler).serve_forever()
