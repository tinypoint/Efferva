"""Translate product authentication into Efferva principals."""

from __future__ import annotations

import asyncio
import re

from fastapi import Request

from efferva import Principal, UnauthenticatedError
from src.config.accessor import get_env_config

_DEV_USER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
_IAP_ISSUER = "https://cloud.google.com/iap"


async def resolve_principal(request: Request) -> Principal:
    """Resolve a local demo cookie or a verified GCP IAP assertion."""
    config = get_env_config().api
    mode = config.product_auth_mode.strip().lower()
    if mode == "dev":
        subject = request.cookies.get("vibe_dev_user", config.product_dev_user)
        if not _DEV_USER_PATTERN.fullmatch(subject):
            raise UnauthenticatedError("invalid development user")
        return Principal(
            tenant_id=config.product_tenant_id,
            issuer="vibe-trading:development",
            subject=subject,
        )
    if mode != "iap":
        raise RuntimeError(f"unsupported Vibe-Trading product auth mode: {mode}")

    assertion = request.headers.get("x-goog-iap-jwt-assertion")
    if not assertion:
        raise UnauthenticatedError("IAP assertion is missing")
    if not config.product_iap_audience:
        raise RuntimeError("VIBE_TRADING_PRODUCT_IAP_AUDIENCE must be configured")

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        claims = await asyncio.to_thread(
            id_token.verify_token,
            assertion,
            google_requests.Request(),
            audience=config.product_iap_audience,
            certs_url=_IAP_CERTS_URL,
        )
    except Exception as error:
        raise UnauthenticatedError("invalid IAP assertion") from error

    if claims.get("iss") != _IAP_ISSUER:
        raise UnauthenticatedError("invalid IAP issuer")
    subject = str(claims.get("sub", "")).strip()
    if not subject:
        raise UnauthenticatedError("IAP subject is missing")
    return Principal(
        tenant_id=config.product_tenant_id,
        issuer=_IAP_ISSUER,
        subject=subject,
    )
