"""Shared pytest fixtures."""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from joserfc import jwt
from joserfc.jwk import RSAKey
from starlette.testclient import TestClient

ISSUER = "https://idp.test/realms/test"
AUDIENCE = "wger-mcp-test"
JWKS_URI = f"{ISSUER}/protocol/openid-connect/certs"
TOKEN_ENDPOINT = f"{ISSUER}/protocol/openid-connect/token"
AUTHORIZATION_ENDPOINT = f"{ISSUER}/protocol/openid-connect/auth"

# Env that, with MCP_AUTH=oidc, satisfies config validation. Explicit JWKS/token
# endpoints skip the discovery network call. Tests override via make_client().
OIDC_ENV = {
    "MCP_AUTH": "oidc",
    "OIDC_ISSUER": ISSUER,
    "OIDC_JWKS_URI": JWKS_URI,
    "OIDC_TOKEN_ENDPOINT": TOKEN_ENDPOINT,
    "OIDC_AUTHORIZATION_ENDPOINT": AUTHORIZATION_ENDPOINT,
    "OIDC_CLIENT_ID": "wger-mcp",
    "OIDC_CLIENT_SECRET": "shh",
    "WGER_OIDC_AUDIENCE": "wger",
    "MCP_OIDC_AUDIENCE": AUDIENCE,
    "MCP_OIDC_USERNAME_CLAIM": "preferred_username",
    "MCP_OIDC_ALLOWED_USERS": "alice",
}

_CLEARED_VARS = (
    "MCP_AUTH",
    "OIDC_ISSUER",
    "OIDC_JWKS_URI",
    "OIDC_TOKEN_ENDPOINT",
    "OIDC_AUTHORIZATION_ENDPOINT",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "WGER_OIDC_AUDIENCE",
    "WGER_ALLAUTH_PROVIDER",
    "MCP_OIDC_AUDIENCE",
    "MCP_OIDC_ALGORITHMS",
    "MCP_OIDC_USERNAME_CLAIM",
    "MCP_OIDC_ALLOWED_USERS",
    "WGER_DEV_TOKEN",
    "MCP_STATIC_TOKEN",
    "MCP_PUBLIC_URL",
    "ALLOWED_HOSTS",
    "DEFAULT_LANGUAGE",
    "MCP_TOOLS",
)


@pytest.fixture(autouse=True)
def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Common upstream config. Each test then sets MCP_AUTH and friends."""
    monkeypatch.setenv("WGER_BASE_URL", "https://wger.test")
    for var in _CLEARED_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def rsa_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": "test-1", "use": "sig"})


@pytest.fixture
def jwks_dict(rsa_key: RSAKey) -> dict[str, Any]:
    pub = rsa_key.as_dict(private=False)
    pub.setdefault("alg", "RS256")
    return {"keys": [pub]}


def make_token(
    key: RSAKey,
    *,
    sub: str = "uuid-alice",
    preferred_username: str = "alice",
    aud: str | list[str] = AUDIENCE,
    iss: str = ISSUER,
    exp_offset: int = 300,
    extra: dict | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "iss": iss,
        "sub": sub,
        "aud": aud,
        "iat": now,
        "exp": now + exp_offset,
        "preferred_username": preferred_username,
    }
    if extra:
        claims.update(extra)
    header = {"alg": "RS256", "kid": key.kid, "typ": "JWT"}
    return jwt.encode(header, claims, key)


@pytest.fixture
def mock_jwks(jwks_dict: dict[str, Any]) -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.get(JWKS_URI).respond(json=jwks_dict)
        yield router


def make_client(**overrides: str) -> TestClient:
    """Build a TestClient with the given env overrides applied to the current process."""
    import os

    for k, v in overrides.items():
        os.environ[k] = v

    from wger_mcp.config import load_settings
    from wger_mcp.server import build_app

    app = build_app(load_settings())
    return TestClient(app, base_url="http://localhost")
