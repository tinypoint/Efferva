from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from importlib.resources import files
from pathlib import Path
from typing import Any

_RUNTIME_NAME = "efferva-codex-runtime.exe" if os.name == "nt" else "efferva-codex-runtime"


class RuntimeBinaryNotFoundError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeBuildInfo:
    efferva_version: str
    codex_revision: str
    runtime_sha256: str
    runtime_target: str
    efferva_revision: str | None = None


def locate_runtime_binary(explicit: str | Path | None = None) -> Path:
    """Locate the runtime override or the executable bundled in the platform wheel."""

    if explicit is not None:
        return _validate_runtime(Path(explicit).expanduser(), source="configured override")

    bundled = files("efferva").joinpath("bin", _RUNTIME_NAME)
    if bundled.is_file():
        return _validate_runtime(Path(str(bundled)), source="installed Efferva wheel")

    system = platform.system() or "unknown-os"
    machine = platform.machine() or "unknown-architecture"
    raise RuntimeBinaryNotFoundError(
        "No bundled Efferva Codex Runtime is available for "
        f"{system}/{machine}. Install a platform-specific efferva wheel, or use the "
        "advanced EFFERVA_RUNTIME_BINARY override. End users must not build Codex at startup."
    )


def runtime_build_info() -> RuntimeBuildInfo | None:
    """Return traceability metadata embedded in a published wheel."""

    package_metadata = files("efferva").joinpath("_build_info.json")
    if package_metadata.is_file():
        return _parse_build_info(package_metadata.read_text(encoding="utf-8"))

    try:
        installed = distribution("efferva")
    except PackageNotFoundError:
        return None
    metadata = next(
        (
            entry
            for entry in installed.files or ()
            if str(entry).endswith(".dist-info/extra_metadata/efferva-build.json")
        ),
        None,
    )
    if metadata is None:
        return None
    return _parse_build_info(installed.locate_file(metadata).read_text(encoding="utf-8"))


def _validate_runtime(path: Path, *, source: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_file():
        raise RuntimeBinaryNotFoundError(f"Efferva runtime {source} does not exist: {resolved}")
    if os.name != "nt" and not os.access(resolved, os.X_OK):
        raise RuntimeBinaryNotFoundError(f"Efferva runtime {source} is not executable: {resolved}")
    return resolved


def _parse_build_info(value: str) -> RuntimeBuildInfo:
    payload: dict[str, Any] = json.loads(value)
    return RuntimeBuildInfo(
        efferva_version=str(payload["efferva_version"]),
        codex_revision=str(payload["codex_revision"]),
        runtime_sha256=str(payload["runtime_sha256"]),
        runtime_target=str(payload["runtime_target"]),
        efferva_revision=(
            str(payload["efferva_revision"])
            if payload.get("efferva_revision") is not None
            else None
        ),
    )
