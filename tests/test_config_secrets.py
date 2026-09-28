"""The credential settings must not reach a log in the clear.

Two separate holes, and SecretStr only closes one of them:

- Anything that formats the settings object — a log line, a traceback frame —
  used to print the values. SecretStr masks them.
- A ValidationError carries the raw input dict as ``input_value`` and truncates
  only its middle, so the tail of WGER_API_KEY survived into the message that
  goes to stderr at startup. SecretStr cannot help there: the dict holds what
  the env source read, before any field was validated. load_settings restates
  such an error as ConfigError, messages only.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from wger_mcp.auth import build_auth_middleware, build_token_provider
from wger_mcp.config import MIN_STATIC_TOKEN_LENGTH, ConfigError, Settings, load_settings

API_KEY = "wger-api-key-11112222333344445555"
STATIC_TOKEN = "s" * MIN_STATIC_TOKEN_LENGTH
CLIENT_SECRET = "idp-client-secret-99998888777766665555"
SECRET_VARS = [
    ("WGER_API_KEY", API_KEY),
    ("MCP_STATIC_TOKEN", STATIC_TOKEN),
    ("OIDC_CLIENT_SECRET", CLIENT_SECRET),
]


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "wger_base_url": "https://wger.test",
        "mcp_auth": "none",
        "wger_dev_token": API_KEY,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# ---------- the startup error ----------


@pytest.mark.parametrize(("var", "value"), SECRET_VARS)
def test_startup_error_does_not_carry_the_secret(
    monkeypatch: pytest.MonkeyPatch, var: str, value: str
) -> None:
    """MCP_AUTH=oidc without its variables is the error operators actually hit."""
    monkeypatch.setenv("MCP_AUTH", "oidc")
    monkeypatch.setenv(var, value)

    with pytest.raises(ConfigError) as exc:
        load_settings(env_file=None)

    message = str(exc.value)
    assert "MCP_AUTH=oidc requires" in message
    assert value not in message
    # The tail is the part that survived Pydantic's truncation.
    assert value[-12:] not in message


def test_the_original_error_is_not_chained(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raised inside the handler, the ValidationError would hang off
    __context__ with the input dict still on it — invisible in the printed
    traceback, but there for anything that reads the chain."""
    monkeypatch.setenv("MCP_AUTH", "oidc")
    monkeypatch.setenv("WGER_API_KEY", API_KEY)

    with pytest.raises(ConfigError) as exc:
        load_settings(env_file=None)

    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_a_valid_configuration_still_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_AUTH", "none")
    monkeypatch.setenv("WGER_API_KEY", API_KEY)

    settings = load_settings(env_file=None)

    assert settings.wger_dev_token is not None
    assert settings.wger_dev_token.get_secret_value() == API_KEY


# ---------- formatting the settings object ----------


@pytest.mark.parametrize("field", ["wger_dev_token", "mcp_static_token", "oidc_client_secret"])
def test_repr_does_not_carry_the_secret(field: str) -> None:
    value = "repr-leak-canary-0123456789"
    settings = _settings(**{field: value})
    for rendered in (repr(settings), str(settings), f"{settings}"):
        assert value not in rendered
        assert "**********" in rendered


def test_secret_fields_are_secret_str() -> None:
    settings = _settings(mcp_static_token=STATIC_TOKEN, oidc_client_secret=CLIENT_SECRET)
    assert isinstance(settings.wger_dev_token, SecretStr)
    assert isinstance(settings.mcp_static_token, SecretStr)
    assert isinstance(settings.oidc_client_secret, SecretStr)


# ---------- the value still has to arrive ----------


def test_the_wger_key_reaches_the_outbound_provider() -> None:
    provider = build_token_provider(_settings())
    assert provider._dev_token == API_KEY


def test_the_static_token_reaches_the_middleware() -> None:
    _, kwargs = build_auth_middleware(
        _settings(mcp_auth="static_token", mcp_static_token=STATIC_TOKEN)
    )
    assert kwargs["token"] == STATIC_TOKEN


# ---------- the length check must measure the value, not the mask ----------


def test_long_static_token_is_accepted() -> None:
    """str(SecretStr) is ten asterisks whatever the value, so a check written
    against it would reject every token as too short."""
    settings = _settings(mcp_auth="static_token", mcp_static_token="a" * 64)
    assert settings.mcp_static_token is not None
    assert len(settings.mcp_static_token.get_secret_value()) == 64


def test_short_static_token_is_still_rejected() -> None:
    with pytest.raises(ValueError, match="at least"):
        _settings(mcp_auth="static_token", mcp_static_token="short")
