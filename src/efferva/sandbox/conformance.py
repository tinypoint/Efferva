from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from efferva.sandbox.types import (
    ProcessHandle,
    ProcessSpec,
    SandboxContext,
    SandboxProvider,
    SandboxRuntime,
)


@dataclass(frozen=True, slots=True)
class ProviderConformanceReport:
    provider: str
    checks: tuple[str, ...]


async def run_provider_conformance(
    provider: SandboxProvider,
    context: SandboxContext | None = None,
) -> ProviderConformanceReport:
    """Run the reusable Coding Agent contract against one provider."""

    context = context or SandboxContext(
        session_id=uuid4(),
    )
    checks: list[str] = []
    volume = await provider.ensure_session_volume(context)
    sandbox = None
    try:
        repeated_volume = await provider.ensure_session_volume(context)
        assert repeated_volume.external_ref == volume.external_ref
        assert volume.provider == provider.name
        checks.append("session-volume-idempotency")

        assert provider.capabilities.coding_agent_compatible
        checks.append("capability-negotiation")

        sandbox = await provider.start(context, volume)
        repeated_sandbox = await provider.start(context, volume)
        assert repeated_sandbox.external_ref == sandbox.external_ref
        runtime = await provider.connect(sandbox)
        checks.append("sandbox-idempotency")

        proof_path = f"{context.workspace_path}/provider-conformance.txt"
        await runtime.write_file(proof_path, b"persistent-workspace")
        assert await runtime.read_file(proof_path) == b"persistent-workspace"
        metadata = await runtime.stat(proof_path)
        assert metadata.is_file and metadata.size == len(b"persistent-workspace")
        entries = await runtime.list_directory(context.workspace_path)
        assert any(entry.name == "provider-conformance.txt" for entry in entries)
        checks.append("filesystem")

        streaming = await runtime.start_process(
            ProcessSpec(
                argv=(
                    "/bin/sh",
                    "-c",
                    "printf stdout-one; sleep 0.1; printf stderr-two >&2",
                ),
                cwd=context.workspace_path,
            )
        )
        stdout, stderr, exit_code = await _collect(runtime, streaming)
        assert stdout == b"stdout-one"
        assert stderr == b"stderr-two"
        assert exit_code == 0
        checks.append("streaming-exec")

        stdin_process = await runtime.start_process(
            ProcessSpec(
                argv=(
                    "/bin/sh",
                    "-c",
                    'IFS= read -r line; printf "stdin:%s" "$line"',
                ),
                cwd=context.workspace_path,
                pipe_stdin=True,
            )
        )
        await runtime.write_stdin(stdin_process, b"accepted\n")
        stdout, _, exit_code = await _collect(runtime, stdin_process)
        assert stdout == b"stdin:accepted"
        assert exit_code == 0
        checks.append("stdin")

        first = await runtime.start_process(
            ProcessSpec(
                argv=("/bin/sh", "-c", "sleep 0.1; printf first"),
                cwd=context.workspace_path,
            )
        )
        second = await runtime.start_process(
            ProcessSpec(
                argv=("/bin/sh", "-c", "sleep 0.1; printf second"),
                cwd=context.workspace_path,
            )
        )
        first_result, second_result = await asyncio.gather(
            _collect(runtime, first),
            _collect(runtime, second),
        )
        assert first_result[0] == b"first"
        assert second_result[0] == b"second"
        checks.append("concurrent-processes")

        if provider.capabilities.interactive_pty:
            pty_process = await runtime.start_process(
                ProcessSpec(
                    argv=("/bin/sh", "-c", "printf pty; IFS= read -r _"),
                    cwd=context.workspace_path,
                    tty=True,
                    pipe_stdin=True,
                )
            )
            await runtime.resize_pty(pty_process, 100, 40)
            await runtime.write_stdin(pty_process, b"\n")
            stdout, _, exit_code = await _collect(runtime, pty_process)
            assert b"pty" in stdout
            assert exit_code == 0
            checks.append("interactive-pty")

        long_running = await runtime.start_process(
            ProcessSpec(
                argv=("/bin/sh", "-c", "sleep 30"),
                cwd=context.workspace_path,
            )
        )
        await runtime.terminate_process(long_running)
        _, _, exit_code = await _collect(runtime, long_running, deadline_seconds=10)
        assert exit_code is not None
        checks.append("process-termination")

        await provider.stop(sandbox)
        sandbox = await provider.start(context, volume)
        runtime = await provider.connect(sandbox)
        assert await runtime.read_file(proof_path) == b"persistent-workspace"
        checks.append("stop-start-persistence")

        return ProviderConformanceReport(provider=provider.name, checks=tuple(checks))
    finally:
        if sandbox is not None:
            await provider.destroy(sandbox)
        destroy_volume = getattr(provider, "destroy_session_volume", None)
        if destroy_volume is not None:
            await destroy_volume(volume)


async def _collect(
    runtime: SandboxRuntime,
    process: ProcessHandle,
    *,
    deadline_seconds: float = 30,
) -> tuple[bytes, bytes, int | None]:
    cursor = 0
    stdout = bytearray()
    stderr = bytearray()
    async with asyncio.timeout(deadline_seconds):
        while True:
            output = await runtime.read_process(process, cursor)
            for chunk in output.chunks:
                cursor = max(cursor, chunk.seq)
                if chunk.stream == "stderr":
                    stderr.extend(chunk.data)
                else:
                    stdout.extend(chunk.data)
            if output.exited:
                return bytes(stdout), bytes(stderr), output.exit_code
            await asyncio.sleep(0.01)
