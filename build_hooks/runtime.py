from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from packaging.tags import sys_tags

_RUNTIME_NAME = "agentframe-codex-runtime"
_PLATFORM_TAG_PATTERN = re.compile(r"^[a-zA-Z0-9_.]+$")


class RuntimeBuildHook(BuildHookInterface):
    """Build a platform wheel containing the precompiled Codex runtime."""

    PLUGIN_NAME = "runtime"

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if version == "editable":
            return

        runtime_value = os.environ.get("AGENTFRAME_BUILD_RUNTIME_BINARY")
        if not runtime_value:
            raise RuntimeError(
                "AGENTFRAME_BUILD_RUNTIME_BINARY must point to a precompiled "
                "agentframe-codex-runtime when building a wheel"
            )
        runtime = Path(runtime_value).expanduser().resolve()
        if not runtime.is_file():
            raise RuntimeError(f"AgentFrame runtime binary does not exist: {runtime}")
        if not os.access(runtime, os.X_OK):
            raise RuntimeError(f"AgentFrame runtime binary is not executable: {runtime}")

        platform_tag = os.environ.get("AGENTFRAME_BUILD_PLATFORM_TAG")
        if platform_tag is None:
            platform_tag = _default_platform_tag()
        if not _PLATFORM_TAG_PATTERN.fullmatch(platform_tag):
            raise RuntimeError(f"invalid wheel platform tag: {platform_tag!r}")

        codex_revision = os.environ.get("AGENTFRAME_BUILD_CODEX_REVISION")
        if not codex_revision:
            raise RuntimeError(
                "AGENTFRAME_BUILD_CODEX_REVISION is required for traceable wheel builds"
            )

        build_info = {
            "agentframe_version": self.metadata.version,
            "codex_revision": codex_revision,
            "runtime_sha256": _sha256(runtime),
            "runtime_target": platform_tag,
        }
        agentframe_revision = os.environ.get("AGENTFRAME_BUILD_REVISION")
        if agentframe_revision:
            build_info["agentframe_revision"] = agentframe_revision

        metadata_path = Path(self.directory, "agentframe-build.json")
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(build_info, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{platform_tag}"
        force_include = build_data["force_include"]
        assert isinstance(force_include, dict)
        force_include[str(runtime)] = f"agentframe/bin/{_RUNTIME_NAME}"
        force_include[str(metadata_path)] = "agentframe/_build_info.json"
        extra_metadata = build_data["extra_metadata"]
        assert isinstance(extra_metadata, dict)
        extra_metadata[str(metadata_path)] = "agentframe-build.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _default_platform_tag() -> str:
    if not sys.platform.startswith("linux"):
        return next(sys_tags()).platform

    architecture = platform.machine().lower()
    architecture = {
        "amd64": "x86_64",
        "arm64": "aarch64",
    }.get(architecture, architecture)
    return f"linux_{architecture}"
