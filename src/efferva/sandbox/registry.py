from __future__ import annotations

from collections.abc import Callable
from typing import Any

from efferva.sandbox.types import SandboxProvider

ProviderFactory = Callable[[], SandboxProvider]

_custom_providers: dict[str, ProviderFactory | type[SandboxProvider]] = {}


def register_sandbox_provider(
    name: str,
    provider: ProviderFactory | type[SandboxProvider],
) -> None:
    normalized = name.strip().lower()
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789-_"
    if not normalized or any(character not in allowed for character in normalized):
        raise ValueError(
            "sandbox provider name must contain only lowercase letters, digits, '-' or '_'"
        )
    if normalized == "opensandbox":
        raise ValueError(f"{normalized} is a reserved first-party sandbox provider")
    _custom_providers[normalized] = provider


def create_registered_provider(name: str) -> SandboxProvider:
    registration = _custom_providers.get(name)
    if registration is None:
        raise ValueError(f"unsupported sandbox provider: {name}")
    provider: Any = registration()
    if provider.name != name:
        raise ValueError(
            f"registered sandbox provider {name!r} returned provider named {provider.name!r}"
        )
    return provider
