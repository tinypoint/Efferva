from __future__ import annotations

from uuid import uuid4

import pytest

from efferva.artifacts import create_publish_artifact_tool
from efferva.sandbox import (
    FileMetadata,
    SandboxEnvironment,
    SandboxFiles,
    SandboxHandle,
)
from efferva.tools import ToolContext


class _Runtime:
    async def stat(self, path: str) -> FileMetadata:
        is_file = path.endswith(".md")
        return FileMetadata(
            is_directory=not is_file,
            is_file=is_file,
            is_symlink=False,
            size=6 if is_file else 0,
            created_at_ms=0,
            modified_at_ms=0,
        )

    async def read_file(self, _path: str) -> bytes:
        return b"report"


class _Repository:
    def __init__(self) -> None:
        self.published = None
        self.events = []

    async def publish_artifact(self, **values):
        self.published = values
        return {
            "id": uuid4(),
            "run_id": values["run_id"],
            "thread_id": values["thread_id"],
            "session_id": values["session_id"],
            "path": values["path"],
            "name": values["name"],
            "media_type": values["media_type"],
            "size_bytes": len(values["content"]),
            "sha256": values["sha256"],
        }

    async def append_event(self, run_id, event, **fence) -> None:
        self.events.append((run_id, event, fence))


@pytest.mark.asyncio
async def test_publish_artifact_uses_fenced_workspace_file_access() -> None:
    repository = _Repository()

    async def current_fence() -> bool:
        return True

    run_id = uuid4()
    thread_id = uuid4()
    session_id = uuid4()
    tool = create_publish_artifact_tool(repository, max_bytes=1024)
    context = ToolContext(
        thread_id="codex-thread",
        turn_id="turn",
        call_id="call",
        sandbox=SandboxEnvironment(
            environment_id="environment",
            endpoint="ws://executor",
            workspace_path="/workspace",
            sandbox=SandboxHandle(
                provider="test",
                external_ref="sandbox",
                workspace_id=session_id,
            ),
            files=SandboxFiles(runtime=_Runtime(), validate_fence=current_fence),
        ),
        run_id=run_id,
        app_thread_id=thread_id,
        session_id=session_id,
        worker_owner_id="worker",
        fencing_epoch=3,
    )

    artifact = await tool.invoke(
        context,
        {"path": "reports/final.md", "name": "final.md"},
    )

    assert artifact["path"] == "/workspace/reports/final.md"
    assert artifact["media_type"] == "text/markdown"
    assert repository.published["owner_id"] == "worker"
    assert repository.events[0][1]["type"] == "ARTIFACT_PUBLISHED"


@pytest.mark.asyncio
async def test_publish_artifact_rejects_paths_outside_workspace() -> None:
    repository = _Repository()
    tool = create_publish_artifact_tool(repository, max_bytes=1024)

    async def current_fence() -> bool:
        return True

    context = ToolContext(
        thread_id="codex-thread",
        turn_id="turn",
        call_id="call",
        sandbox=SandboxEnvironment(
            environment_id="environment",
            endpoint="ws://executor",
            workspace_path="/workspace",
            sandbox=SandboxHandle(
                provider="test",
                external_ref="sandbox",
                workspace_id=uuid4(),
            ),
            files=SandboxFiles(runtime=_Runtime(), validate_fence=current_fence),
        ),
        run_id=uuid4(),
        app_thread_id=uuid4(),
        session_id=uuid4(),
        worker_owner_id="worker",
        fencing_epoch=1,
    )

    with pytest.raises(ValueError, match="inside the Session workspace"):
        await tool.invoke(context, {"path": "../secret"})
