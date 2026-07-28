from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from efferva.runtime_binary import (
    RuntimeBinaryNotFoundError,
    locate_runtime_binary,
    runtime_build_info,
)


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_explicit_runtime_override_has_priority(tmp_path: Path) -> None:
    runtime = _executable(tmp_path / "runtime")

    assert locate_runtime_binary(runtime) == runtime.resolve()


def test_bundled_runtime_is_located_without_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "efferva"
    runtime = _executable(package / "bin" / "efferva-codex-runtime")
    monkeypatch.setattr("efferva.runtime_binary.files", lambda _: package)

    assert locate_runtime_binary() == runtime.resolve()


def test_missing_runtime_reports_platform_and_remediation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("efferva.runtime_binary.files", lambda _: tmp_path)

    with pytest.raises(
        RuntimeBinaryNotFoundError,
        match="Install a platform-specific efferva wheel",
    ):
        locate_runtime_binary()


def test_wheel_build_metadata_is_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    package = tmp_path / "efferva"
    package.mkdir()
    (package / "_build_info.json").write_text(
        json.dumps(
            {
                "efferva_version": "0.1.0",
                "efferva_revision": "framework-revision",
                "codex_revision": "codex-revision",
                "runtime_sha256": "sha256",
                "runtime_target": "linux_aarch64",
            }
        )
    )
    monkeypatch.setattr("efferva.runtime_binary.files", lambda _: package)

    info = runtime_build_info()

    assert info is not None
    assert info.codex_revision == "codex-revision"
    assert info.runtime_target == "linux_aarch64"


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permission semantics")
def test_non_executable_override_is_rejected(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_text("not executable")

    with pytest.raises(RuntimeBinaryNotFoundError, match="not executable"):
        locate_runtime_binary(runtime)
