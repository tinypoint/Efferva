from __future__ import annotations

import hashlib
import mimetypes
import posixpath
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from efferva.events import artifact_published
from efferva.repository import SystemRepository
from efferva.sandbox import FileMetadata, SandboxFiles
from efferva.tools import Tool, ToolContext


def create_publish_artifact_tool(
    repository: SystemRepository,
    *,
    max_bytes: int,
) -> Tool:
    """Create the built-in, lease-fenced artifact publication tool."""

    async def publish(
        context: ToolContext,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        if (
            context.run_id is None
            or context.app_thread_id is None
            or context.session_id is None
            or context.worker_owner_id is None
            or context.fencing_epoch is None
        ):
            raise RuntimeError("publish_artifact requires an active Efferva Run")
        if context.sandbox.files is None:
            raise RuntimeError("sandbox provider does not expose fenced file access")

        path_value = arguments.get("path")
        if not isinstance(path_value, str) or not path_value.strip():
            raise ValueError("path is required")
        if len(path_value) > 4096:
            raise ValueError("artifact path is too long")
        path = _workspace_path(context.sandbox.workspace_path, path_value)
        metadata = await _stat_workspace_file(
            context.sandbox.files,
            context.sandbox.workspace_path,
            path,
        )
        if not metadata.is_file or metadata.is_symlink:
            raise ValueError("artifact path must be a regular file")
        if metadata.size > max_bytes:
            raise ValueError(
                f"artifact exceeds the {max_bytes}-byte publication limit"
            )
        content = await context.sandbox.files.read_file(path)
        if len(content) > max_bytes:
            raise ValueError(
                f"artifact exceeds the {max_bytes}-byte publication limit"
            )

        name_value = arguments.get("name")
        name = (
            name_value.strip()
            if isinstance(name_value, str) and name_value.strip()
            else PurePosixPath(path).name
        )
        if "/" in name or "\\" in name or "\r" in name or "\n" in name:
            raise ValueError("artifact name must be a plain filename")
        if len(name) > 255:
            raise ValueError("artifact name is too long")
        media_type_value = arguments.get("media_type")
        media_type = (
            media_type_value.strip()
            if isinstance(media_type_value, str) and media_type_value.strip()
            else mimetypes.guess_type(name)[0] or "application/octet-stream"
        )
        if len(media_type) > 255 or "\r" in media_type or "\n" in media_type:
            raise ValueError("artifact media_type is invalid")
        digest = hashlib.sha256(content).hexdigest()
        artifact = await repository.publish_artifact(
            run_id=context.run_id,
            thread_id=context.app_thread_id,
            session_id=context.session_id,
            path=path,
            name=name,
            media_type=media_type,
            content=content,
            sha256=digest,
            owner_id=context.worker_owner_id,
            fencing_epoch=context.fencing_epoch,
        )
        await repository.append_event(
            context.run_id,
            artifact_published(
                artifact["id"],
                path=path,
                name=name,
                media_type=media_type,
                size_bytes=len(content),
                sha256=digest,
            ),
            owner_id=context.worker_owner_id,
            fencing_epoch=context.fencing_epoch,
        )
        return artifact

    return Tool(
        name="publish_artifact",
        description=(
            "Publish a file from the current Session workspace as a durable Run artifact. "
            "Use this for reports, charts, datasets, and other files the user should download."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Absolute workspace path or path relative to "
                        "/session/workspace."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Optional user-facing filename.",
                },
                "media_type": {
                    "type": "string",
                    "description": "Optional MIME type.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=publish,
    )


def _workspace_path(workspace_path: str, value: str) -> str:
    if value.startswith("/"):
        normalized = posixpath.normpath(value)
    else:
        normalized = posixpath.normpath(posixpath.join(workspace_path, value))
    root = workspace_path.rstrip("/")
    if normalized != root and not normalized.startswith(f"{root}/"):
        raise ValueError("artifact path must stay inside the Session workspace")
    return normalized


async def _stat_workspace_file(
    files: SandboxFiles,
    workspace_path: str,
    path: str,
) -> FileMetadata:
    root = PurePosixPath(workspace_path)
    relative = PurePosixPath(path).relative_to(root)
    current = root
    metadata: FileMetadata | None = None
    for index, part in enumerate(relative.parts):
        current /= part
        metadata = await files.stat(str(current))
        if metadata.is_symlink:
            raise ValueError("artifact path must not traverse symbolic links")
        if index < len(relative.parts) - 1 and not metadata.is_directory:
            raise ValueError("artifact parent path must contain only directories")
    if metadata is None:
        raise ValueError("artifact path must identify a file")
    return metadata
