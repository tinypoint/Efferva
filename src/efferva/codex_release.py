from __future__ import annotations

import asyncio
import hashlib
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from efferva.config import Settings

_DEFAULT_VERSION = "0.146.0"
_DEFAULT_ARCHIVE_SHA256 = {
    "aarch64-unknown-linux-musl": (
        "975bac91562abeedeb8f79636d51a86649b31f34a9de6a3bcb059565b6cf1f87"
    ),
    "x86_64-unknown-linux-musl": (
        "5ba3b9405543953081f661d0854d266f76e2abbe51d41349355a36de7673776a"
    ),
}
_prepare_lock = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class OfficialCodex:
    version: str
    target: str
    binary: Path
    binary_sha256: str


class CodexReleaseError(RuntimeError):
    pass


async def prepare_official_codex(settings: Settings) -> OfficialCodex:
    """Download and verify the pinned official Codex CLI used by Session sandboxes."""

    version = settings.codex_version.removeprefix("rust-v").removeprefix("v")
    target = settings.codex_release_target or _default_linux_target()
    archive_sha256 = settings.codex_archive_sha256
    if archive_sha256 is None and version == _DEFAULT_VERSION:
        archive_sha256 = _DEFAULT_ARCHIVE_SHA256.get(target)
    if archive_sha256 is None:
        raise CodexReleaseError(
            "EFFERVA_CODEX_ARCHIVE_SHA256 is required when selecting a Codex "
            "version or target other than Efferva's pinned default"
        )
    archive_sha256 = archive_sha256.removeprefix("sha256:").lower()

    async with _prepare_lock:
        return await asyncio.to_thread(
            _prepare,
            version,
            target,
            archive_sha256,
            settings.codex_release_cache_dir,
        )


def _prepare(
    version: str,
    target: str,
    archive_sha256: str,
    cache_root: Path,
) -> OfficialCodex:
    release_dir = cache_root.expanduser() / version / target
    binary = release_dir / "codex"
    digest_file = release_dir / "codex.sha256"
    if binary.is_file() and digest_file.is_file():
        binary_sha256 = digest_file.read_text(encoding="utf-8").strip()
        if binary_sha256 == _sha256(binary):
            return OfficialCodex(version, target, binary, binary_sha256)

    release_dir.mkdir(parents=True, exist_ok=True)
    asset_name = f"codex-{target}.tar.gz"
    url = (
        "https://github.com/openai/codex/releases/download/"
        f"rust-v{version}/{asset_name}"
    )
    archive_fd, archive_name = tempfile.mkstemp(
        prefix=f".{asset_name}.",
        dir=release_dir,
    )
    os.close(archive_fd)
    archive = Path(archive_name)
    binary_fd, binary_name = tempfile.mkstemp(prefix=".codex.", dir=release_dir)
    os.close(binary_fd)
    temporary_binary = Path(binary_name)
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Efferva/0.1 official-codex-fetcher"},
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            with archive.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
        actual_archive_sha256 = _sha256(archive)
        if actual_archive_sha256 != archive_sha256:
            raise CodexReleaseError(
                f"Official Codex archive checksum mismatch for {asset_name}: "
                f"expected {archive_sha256}, got {actual_archive_sha256}"
            )

        with tarfile.open(archive, mode="r:gz") as bundle:
            expected_names = {"codex", f"codex-{target}"}
            members = [
                member
                for member in bundle.getmembers()
                if member.isfile() and Path(member.name).name in expected_names
            ]
            if len(members) != 1:
                raise CodexReleaseError(
                    f"Official Codex archive {asset_name} did not contain one Codex binary"
                )
            source = bundle.extractfile(members[0])
            if source is None:
                raise CodexReleaseError(
                    f"Could not read Codex binary from official archive {asset_name}"
                )
            with source, temporary_binary.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)

        binary_sha256 = _sha256(temporary_binary)
        temporary_binary.chmod(0o755)
        os.replace(temporary_binary, binary)
        digest_file.write_text(binary_sha256 + "\n", encoding="utf-8")
        return OfficialCodex(version, target, binary, binary_sha256)
    finally:
        archive.unlink(missing_ok=True)
        temporary_binary.unlink(missing_ok=True)


def _default_linux_target() -> str:
    machine = platform.machine().lower()
    architecture = {
        "amd64": "x86_64",
        "arm64": "aarch64",
    }.get(machine, machine)
    if architecture not in {"x86_64", "aarch64"}:
        raise CodexReleaseError(
            f"No official Linux Codex asset is configured for architecture {machine}"
        )
    return f"{architecture}-unknown-linux-musl"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
