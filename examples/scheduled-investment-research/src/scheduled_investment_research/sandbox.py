from __future__ import annotations

import asyncio
import posixpath
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import PurePosixPath

from efferva import (
    SandboxCapabilities,
    SandboxContext,
    SandboxEnvironment,
    SandboxRuntime,
)
from efferva.config import Settings, get_settings
from efferva.sandbox.providers.opensandbox import OpenSandboxProvider

AI_BERKSHIRE_REPOSITORY = "https://github.com/xbtlin/ai-berkshire"
AI_BERKSHIRE_REVISION = "0310788cdabb0d724ac9f67e3dbd3e9e4a13d06a"


def _resource_files(
    root: Traversable,
    prefix: PurePosixPath = PurePosixPath(),
) -> list[tuple[PurePosixPath, Traversable]]:
    result: list[tuple[PurePosixPath, Traversable]] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        relative_path = prefix / child.name
        if child.is_dir():
            result.extend(_resource_files(child, relative_path))
        elif child.is_file():
            result.append((relative_path, child))
    return result


class BundledSkillsProvider:
    """Seeds product-owned AI Berkshire resources before Codex starts."""

    def __init__(self, inner: OpenSandboxProvider, settings: Settings) -> None:
        self._inner = inner
        self._settings = settings
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def capabilities(self) -> SandboxCapabilities:
        return self._inner.capabilities

    async def open(self) -> None:
        await self._inner.open()

    async def ensure(self, context: SandboxContext) -> SandboxEnvironment:
        environment = await self._inner.ensure(context)
        lock = self._locks.setdefault(
            environment.sandbox.external_ref,
            asyncio.Lock(),
        )
        async with lock:
            await self._seed(environment.runtime)
        return environment

    async def close(self) -> None:
        self._locks.clear()
        await self._inner.close()

    async def _seed(self, runtime: SandboxRuntime) -> None:
        codex_skills = posixpath.join(
            self._settings.codex_home_path,
            "skills",
        )
        marker = posixpath.join(codex_skills, ".ai-berkshire-revision")
        try:
            await runtime.stat(marker)
        except FileNotFoundError:
            installed_revision = ""
        else:
            installed_revision = (await runtime.read_file(marker)).decode().strip()
        if installed_revision == AI_BERKSHIRE_REVISION:
            return

        package_root = files("scheduled_investment_research")
        skill_files = _resource_files(package_root.joinpath("skills"))
        workspace_files = _resource_files(package_root.joinpath("workspace"))
        workspace = self._settings.workspace_path

        targets = [
            (
                posixpath.join(codex_skills, relative.as_posix()),
                resource,
            )
            for relative, resource in skill_files
        ]
        targets.extend(
            (
                posixpath.join(workspace, relative.as_posix()),
                resource,
            )
            for relative, resource in workspace_files
        )
        directories = sorted(
            {
                codex_skills,
                workspace,
                posixpath.join(workspace, "reports"),
                *(posixpath.dirname(target) for target, _ in targets),
            }
        )
        await self._run_checked(runtime, ("mkdir", "-p", *directories))

        for target, resource in targets:
            await runtime.write_file(target, resource.read_bytes())
        await runtime.write_file(marker, f"{AI_BERKSHIRE_REVISION}\n".encode())

        owner = f"{self._settings.sandbox_uid}:{self._settings.sandbox_gid}"
        await self._run_checked(
            runtime,
            (
                "chown",
                "-R",
                owner,
                codex_skills,
                posixpath.join(workspace, "tools"),
                posixpath.join(workspace, "reports"),
                posixpath.join(workspace, "AGENTS.md"),
                posixpath.join(workspace, "AI_BERKSHIRE_LICENSE"),
            ),
        )

    @staticmethod
    async def _run_checked(
        runtime: SandboxRuntime,
        argv: tuple[str, ...],
    ) -> None:
        result = await runtime.run_command(argv, cwd="/")
        if result.exit_code == 0:
            return
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(detail or f"Sandbox command failed: {argv[0]}")


def create_sandbox_provider() -> BundledSkillsProvider:
    settings = get_settings()
    return BundledSkillsProvider(OpenSandboxProvider(settings), settings)
