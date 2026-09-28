"""StaticTokenMiddleware: config validation, bearer gating, public paths."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from wger_mcp.config import (
    MIN_STATIC_TOKEN_LENGTH,
    AuthStrategy,
    Settings,
    load_settings,
)

from .conftest import make_client

# A secret that clears MIN_STATIC_TOKEN_LENGTH.
TOKEN = "t" * MIN_STATIC_TOKEN_LENGTH
STATIC_ENV = {
    "MCP_AUTH": "static_token",
    "MCP_STATIC_TOKEN": TOKEN,
    "WGER_API_KEY": "wger-api-key",
}

_TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _client(**overrides: str):
    return make_client(**{**STATIC_ENV, **overrides})


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "wger_base_url": "https://wger.test",
        "mcp_auth": "static_token",
        "mcp_static_token": TOKEN,
        "wger_dev_token": "wger-api-key",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------- config validation ----------


def test_static_token_strategy_accepts_valid_config() -> None:
    s = _settings()
    assert s.mcp_auth is AuthStrategy.static_token
    assert s.mcp_static_token is not None
    assert s.mcp_static_token.get_secret_value() == TOKEN


@pytest.mark.parametrize(
    ("missing", "reported"),
    [("mcp_static_token", "MCP_STATIC_TOKEN"), ("wger_dev_token", "WGER_API_KEY")],
)
def test_static_token_requires_both_secrets(missing: str, reported: str) -> None:
    """The message must name the variable the docs tell people to set, which is
    not the field name — those parted ways when WGER_DEV_TOKEN was renamed."""
    with pytest.raises(ValidationError) as exc:
        _settings(**{missing: None})
    assert reported in str(exc.value)


def test_short_static_token_rejected() -> None:
    """A guessable shared secret is the whole attack surface here."""
    with pytest.raises(ValidationError) as exc:
        _settings(mcp_static_token="short")
    assert "at least" in str(exc.value)


def test_token_at_minimum_length_accepted() -> None:
    assert _settings(mcp_static_token="a" * MIN_STATIC_TOKEN_LENGTH).mcp_static_token


# ---------- request gating ----------


def test_missing_bearer_returns_401() -> None:
    with _client() as c:
        r = c.post("/mcp/", json=_TOOLS_LIST)
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer ")


def test_wrong_token_returns_401() -> None:
    with _client() as c:
        r = c.post(
            "/mcp/", json=_TOOLS_LIST, headers={"Authorization": f"Bearer {'x' * len(TOKEN)}"}
        )
        assert r.status_code == 401


def test_non_bearer_scheme_returns_401() -> None:
    with _client() as c:
        r = c.post("/mcp/", json=_TOOLS_LIST, headers={"Authorization": f"Token {TOKEN}"})
        assert r.status_code == 401


def test_correct_token_is_accepted() -> None:
    """A valid secret passes the auth gate (405: GET is not a valid MCP verb,
    which is past the 401 we are asserting against)."""
    with _client() as c:
        r = c.post(
            "/mcp/",
            json=_TOOLS_LIST,
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert r.status_code != 401


def test_health_is_public() -> None:
    with _client() as c:
        assert c.get("/health").status_code == 200


def test_oauth_metadata_not_advertised() -> None:
    """static_token must not advertise OAuth discovery: a client following it
    would run an OAuth flow whose token this server never accepts."""
    with _client(OIDC_ISSUER="https://idp.test/realms/test") as c:
        # The discovery documents are bypass paths, so a 404 here proves the
        # routes were never registered rather than merely being auth-gated.
        assert c.get("/.well-known/oauth-protected-resource").status_code == 404
        assert c.get("/.well-known/oauth-authorization-server").status_code == 404
        # The AS-facade endpoints are not bypass paths under this strategy, so
        # they are refused by the auth gate rather than 404-ing.
        assert c.get("/authorize").status_code == 401


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"WGER_DEV_TOKEN": "legacy"}, "legacy"),
        # AliasChoices order decides, and WGER_API_KEY is listed first.
        ({"WGER_DEV_TOKEN": "legacy", "WGER_API_KEY": "current"}, "current"),
    ],
)
def test_the_pre_rename_variable_name_still_configures_deployments(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str
) -> None:
    """WGER_DEV_TOKEN is no longer documented but must keep working: it is sitting
    in the compose files of everyone who deployed this before the rename."""
    monkeypatch.setenv("MCP_AUTH", "none")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    token = load_settings(env_file=None).wger_dev_token
    assert token is not None
    assert token.get_secret_value() == expected
