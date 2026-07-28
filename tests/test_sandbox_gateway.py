from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from websockets.asyncio.client import connect

from agentframe import AgentFrame
from agentframe.config import Settings
from agentframe.sandbox.conformance import run_provider_conformance
from agentframe.sandbox.gateway import ExecutorGateway
from agentframe.sandbox.manager import create_sandbox_provider
from agentframe.sandbox.runtime import (
    BufferedSandboxRuntime,
    ProcessTransport,
    TransportEvent,
    TransportExited,
    TransportOutput,
)
from agentframe.sandbox.types import (
    ProcessHandle,
    ProcessSpec,
    SandboxCapabilities,
    SandboxContext,
    SandboxHandle,
    WorkspaceHandle,
)


class LocalTransport(ProcessTransport):
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.queue: asyncio.Queue[TransportEvent | None] = asyncio.Queue()
        self.task = asyncio.create_task(self._collect())

    async def _collect(self) -> None:
        async def pump(reader: asyncio.StreamReader | None, stream: str) -> None:
            if reader is None:
                return
            while chunk := await reader.read(64 * 1024):
                await self.queue.put(TransportOutput(stream, chunk))  # type: ignore[arg-type]

        stdout = asyncio.create_task(pump(self.process.stdout, "stdout"))
        stderr = asyncio.create_task(pump(self.process.stderr, "stderr"))
        exit_code = await self.process.wait()
        await asyncio.gather(stdout, stderr)
        await self.queue.put(TransportExited(exit_code))
        await self.queue.put(None)

    async def events(self) -> AsyncIterator[TransportEvent]:
        while (event := await self.queue.get()) is not None:
            yield event

    async def write(self, data: bytes) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def resize(self, cols: int, rows: int) -> None:
        raise RuntimeError("no PTY")

    async def terminate(self) -> None:
        if self.process.returncode is None:
            self.process.terminate()

    async def close(self) -> None:
        await self.task


class LocalRuntime(BufferedSandboxRuntime):
    async def _launch(
        self,
        spec: ProcessSpec,
        handle: ProcessHandle,
    ) -> ProcessTransport:
        process = await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=spec.cwd,
            env={**os.environ, **spec.env},
            stdin=(
                asyncio.subprocess.PIPE
                if spec.pipe_stdin or spec.initial_stdin is not None
                else asyncio.subprocess.DEVNULL
            ),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return LocalTransport(process)


class LocalProvider:
    name = "local-test"
    capabilities = SandboxCapabilities(
        streaming_exec=True,
        interactive_pty=False,
        persistent_workspace=True,
        snapshots=False,
        suspend_resume=True,
        port_forwarding=False,
        network_policy=False,
    )

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.runtime: LocalRuntime | None = None

    async def ensure_workspace(self, context: SandboxContext) -> WorkspaceHandle:
        return WorkspaceHandle(self.name, self.workspace)

    async def start(
        self,
        context: SandboxContext,
        workspace: WorkspaceHandle,
    ) -> SandboxHandle:
        return SandboxHandle(self.name, "local", context.workspace_id)

    async def connect(self, sandbox: SandboxHandle) -> LocalRuntime:
        if self.runtime is None:
            self.runtime = LocalRuntime(self.workspace)
        return self.runtime

    async def stop(self, sandbox: SandboxHandle) -> None:
        if self.runtime is not None:
            await self.runtime.close()
            self.runtime = None

    async def destroy(self, sandbox: SandboxHandle) -> None:
        await self.stop(sandbox)


@pytest.mark.asyncio
async def test_reusable_provider_conformance_suite() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        provider = LocalProvider(workspace)
        context = SandboxContext(
            session_id=uuid4(),
            workspace_id=uuid4(),
            workspace_ref="local-test",
            workspace_path=workspace,
        )
        report = await run_provider_conformance(provider, context)

    assert report.provider == "local-test"
    assert "streaming-exec" in report.checks
    assert "stop-start-persistence" in report.checks


@pytest.mark.asyncio
async def test_executor_gateway_translates_codex_protocol() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        runtime = LocalRuntime(workspace)
        sandbox = SandboxHandle("local-test", "local", uuid4())
        gateway = ExecutorGateway()
        await gateway.start()
        fence_is_valid = True

        async def valid_fence() -> bool:
            return fence_is_valid

        environment = gateway.register(
            environment_id="test-environment",
            runtime=runtime,
            workspace_path=workspace,
            sandbox=sandbox,
            validate_fence=valid_fence,
        )
        try:
            async with connect(environment.endpoint) as websocket:
                request_id = 0

                async def rpc(method: str, params: dict | None = None) -> dict:
                    nonlocal request_id
                    request_id += 1
                    await websocket.send(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "method": method,
                                "params": params or {},
                            }
                        )
                    )
                    while True:
                        response = json.loads(await websocket.recv())
                        if response.get("id") == request_id:
                            assert "error" not in response
                            return response["result"]

                initialized = await rpc(
                    "initialize",
                    {"clientName": "test", "resumeSessionId": None},
                )
                assert initialized == {"sessionId": "test-environment"}
                await websocket.send(
                    json.dumps({"jsonrpc": "2.0", "method": "initialized", "params": {}})
                )
                info = await rpc("environment/info")
                assert info["cwd"] == Path(workspace).as_uri()

                fence_is_valid = False
                request_id += 1
                await websocket.send(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "environment/status",
                            "params": {},
                        }
                    )
                )
                stale = json.loads(await websocket.recv())
                assert stale["error"]["code"] == -32003
                fence_is_valid = True

                await rpc(
                    "process/start",
                    {
                        "processId": "process-1",
                        "argv": ["/bin/sh", "-c", "printf gateway"],
                        "cwd": Path(workspace).as_uri(),
                        "env": {},
                        "tty": False,
                        "pipeStdin": False,
                        "arg0": None,
                    },
                )
                cursor = 0
                output = bytearray()
                while True:
                    result = await rpc(
                        "process/read",
                        {
                            "processId": "process-1",
                            "afterSeq": cursor,
                            "waitMs": 100,
                        },
                    )
                    for chunk in result["chunks"]:
                        cursor = max(cursor, chunk["seq"])
                        output.extend(base64.b64decode(chunk["chunk"]))
                    if result["exited"]:
                        break
                assert bytes(output) == b"gateway"

                path = Path(workspace, "gateway.txt").as_uri()
                await rpc(
                    "fs/writeFile",
                    {
                        "path": path,
                        "dataBase64": base64.b64encode(b"file").decode(),
                    },
                )
                read = await rpc("fs/readFile", {"path": path})
                assert base64.b64decode(read["dataBase64"]) == b"file"
        finally:
            await gateway.close()
            await runtime.close()


def test_product_can_register_custom_provider() -> None:
    with tempfile.TemporaryDirectory() as workspace:

        def provider_factory() -> LocalProvider:
            provider = LocalProvider(workspace)
            provider.name = "company-test"
            return provider

        AgentFrame.register_sandbox_provider(
            "company-test",
            provider_factory,
        )
        provider = create_sandbox_provider(
            Settings(sandbox_provider="company-test"),
        )

        assert provider.name == "company-test"
