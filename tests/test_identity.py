from __future__ import annotations

import pytest

from efferva import Capability, Principal


def test_principal_normalizes_and_types_capabilities() -> None:
    principal = Principal(
        tenant_id=" acme ",
        issuer=" product ",
        subject=" alice ",
        capabilities=frozenset({"sessions:read:tenant"}),
    )

    assert principal.tenant_id == "acme"
    assert principal.issuer == "product"
    assert principal.subject == "alice"
    assert principal.capabilities == frozenset({Capability.SESSIONS_READ_TENANT})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", ""),
        ("issuer", " "),
        ("subject", ""),
        ("tenant_id", "__agentframe_legacy__"),
        ("issuer", "agentframe:legacy"),
    ],
)
def test_principal_rejects_invalid_or_reserved_identity(field: str, value: str) -> None:
    values = {"tenant_id": "acme", "issuer": "product", "subject": "alice"}
    values[field] = value

    with pytest.raises(ValueError):
        Principal(**values)
