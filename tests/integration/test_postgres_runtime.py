from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest


class RuntimeClient:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.next_id = 1

    @classmethod
    async def start(cls, binary: Path, database_url: str) -> RuntimeClient:
        environment = os.environ.copy()
        environment["EFFERVA_DATABASE_URL"] = database_url
        process = await asyncio.create_subprocess_exec(
            str(binary),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        client = cls(process)
        await client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "efferva-test",
                    "title": "Efferva test",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        await client.notify("notifications/initialized")
        return client

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        await self._write({"id": request_id, "method": method, "params": params})
        assert self.process.stdout is not None
        async with asyncio.timeout(30):
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if message.get("id") != request_id:
                    continue
                assert "error" not in message, message
                return message["result"]
        raise AssertionError(f"{method} did not return a response")

    async def notify(self, method: str) -> None:
        await self._write({"method": method})

    async def _write(self, payload: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload).encode() + b"\n")
        await self.process.stdin.drain()

    async def close(self) -> None:
        if self.process.stdin is not None:
            self.process.stdin.close()
            await self.process.stdin.wait_closed()
        await asyncio.wait_for(self.process.wait(), 20)


def _configuration() -> tuple[Path, str]:
    database_url = os.getenv("EFFERVA_TEST_DATABASE_URL")
    binary_value = os.getenv(
        "EFFERVA_TEST_RUNTIME_BINARY",
        "target/debug/efferva-codex-runtime",
    )
    binary = Path(binary_value).resolve()
    if not database_url:
        pytest.skip("EFFERVA_TEST_DATABASE_URL is not set")
    if not binary.exists():
        pytest.skip(f"runtime binary not found: {binary}")
    return binary, database_url


@pytest.mark.integration
async def test_thread_read_list_resume_across_runtime_instances(tmp_path: Path) -> None:
    binary, database_url = _configuration()
    first = await RuntimeClient.start(binary, database_url)
    started = await first.request(
        "thread/start",
        {
            "cwd": str(tmp_path),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        },
    )
    thread_id = started["thread"]["id"]
    await first.close()

    second = await RuntimeClient.start(binary, database_url)
    try:
        listed = await second.request(
            "thread/list",
            {
                "sourceKinds": ["appServer"],
                "modelProviders": [],
                "limit": 100,
            },
        )
        assert thread_id in {thread["id"] for thread in listed["data"]}

        resumed = await second.request("thread/resume", {"threadId": thread_id})
        assert resumed["thread"]["id"] == thread_id
        assert resumed["thread"]["path"] is None

        read = await second.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": True},
        )
        assert read["thread"]["id"] == thread_id
    finally:
        await second.close()
