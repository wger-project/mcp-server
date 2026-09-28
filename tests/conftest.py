"""Shared pytest fixtures."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from joserfc import jwt
from joserfc.jwk import RSAKey
from starlette.testclient import TestClient
from wger_api_client.api.userprofile import userprofile_retrieve
from wger_api_client.models.userprofile import Userprofile

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

#: A ``/userprofile/`` reply the generated model accepts. ``Userprofile
#: .from_dict`` pops eight fields without a default, so a shorter dict raises
#: ``KeyError`` instead of parsing.
PROFILE: dict[str, Any] = {
    "username": "alice",
    "email": "alice@wger.test",
    "email_verified": True,
    "is_trustworthy": False,
    "date_joined": "2026-01-01T00:00:00Z",
    "gym": None,
    "is_temporary": False,
    "last_workout_notification": None,
    "weight_unit": "kg",
}

# Every settings variable has to go, or the developer's shell configures the
# suite. A prefix rule rather than a hand-kept list: keeping the list current is
# exactly what failed when MCP_TRANSPORT and WGER_API_KEY were added, and it had
# silently never covered OAUTH_* either.
_SETTINGS_PREFIXES = ("WGER_", "MCP_", "OIDC_", "OAUTH_")
#: Settings fields whose env var carries no such prefix.
_UNPREFIXED_SETTINGS_VARS = frozenset({"ALLOWED_HOSTS", "DEFAULT_LANGUAGE", "HOST", "PORT"})


def is_settings_var(name: str) -> bool:
    """Whether ``name`` is an env var the settings model would read.

    Compared upper-cased because pydantic-settings matches case-insensitively:
    a lowercase ``mcp_transport`` in the environment configures the server just
    as well, and must not survive into a test.
    """
    upper = name.upper()
    return upper.startswith(_SETTINGS_PREFIXES) or upper in _UNPREFIXED_SETTINGS_VARS


def scrubbed_env(**overrides: str) -> dict[str, str]:
    """A copy of the environment with every settings variable removed.

    For subprocess tests, which inherit the real environment rather than the
    fixture's patched one.
    """
    env = {k: v for k, v in os.environ.items() if not is_settings_var(k)}
    return {**env, **overrides}


@pytest.fixture(autouse=True)
def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Common upstream config. Each test then sets MCP_AUTH and friends."""
    for var in [k for k in os.environ if is_settings_var(k)]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WGER_BASE_URL", "https://wger.test")


@pytest.fixture(autouse=True)
def _skip_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the wger version probe out of every test that boots the app.

    build_app() checks its upstream on startup, so without this each TestClient
    would make a real request — which respx refuses as unmocked and which would
    make the suite depend on the network besides. The probe itself is covered in
    test_compat.py, including that the boot is still wired to it.
    """

    async def _noop(settings: Any) -> None:
        return None

    monkeypatch.setattr("wger_mcp.server.check_wger_version", _noop)


@pytest.fixture(autouse=True)
def profile_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer ``/userprofile/`` for every test, with a kilogram profile.

    The write tools read the trainee's unit whenever the caller omits one, so
    without this any test that omits it would have to mock the endpoint itself.
    A test that cares about the unit patches the same attribute again.
    """

    async def retrieve(**kwargs: Any) -> Userprofile:
        return Userprofile.from_dict(dict(PROFILE))

    monkeypatch.setattr(userprofile_retrieve, "asyncio", retrieve)


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
    for k, v in overrides.items():
        os.environ[k] = v

    from wger_mcp.config import load_settings
    from wger_mcp.server import build_app

    # env_file=None: the fixture can only scrub os.environ, so a developer's
    # ./.env would otherwise reach the settings by a route no test controls.
    app = build_app(load_settings(env_file=None))
    return TestClient(app, base_url="http://localhost")
