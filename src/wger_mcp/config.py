"""Settings for wger-mcp.

Since wger 2.6, the server is **multi-user**: every request acts as the
caller's own wger account. Authentication has two halves that share one
SSO identity provider (any OIDC IdP — Keycloak, Authentik, Auth0, Okta, …):

- **Inbound** — the client presents an OIDC-issued token (via MCP-native OAuth
  or out-of-band). The server validates it against the IdP's JWKS.
- **Outbound** — the server is a *confidential* OIDC client. It exchanges the
  inbound token (RFC 8693) for one whose audience is wger's OIDC client, posts
  that to wger's allauth headless ``provider/token`` endpoint, and uses the
  returned wger JWT as ``Authorization: Bearer`` on the wger REST API. See
  ``docs/adr/0001-multi-user-auth-via-oidc-token-exchange.md``.

Endpoints (JWKS, token) are resolved from the IdP's discovery document
(``{issuer}/.well-known/openid-configuration``) unless overridden.

Two single-user strategies avoid the IdP entirely, both calling wger with a
static ``WGER_DEV_TOKEN`` (a personal DRF API key):

- ``MCP_AUTH=static_token`` — callers must present ``MCP_STATIC_TOKEN`` as a
  bearer token. Inbound requests *are* authenticated, so this is safe to expose
  over TLS; the secret grants full access to that one wger account.
- ``MCP_AUTH=none`` — no inbound authentication at all. Anyone who can reach
  ``/mcp`` acts as the account behind the dev token, so bind it to localhost.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Minimum length for MCP_STATIC_TOKEN. 32 chars is roughly the shortest value
# that is still awkward to brute-force; `openssl rand -hex 32` gives 64.
MIN_STATIC_TOKEN_LENGTH = 32


class AuthStrategy(StrEnum):
    oidc = "oidc"
    static_token = "static_token"
    none = "none"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="",
    )

    # ---------- upstream wger ----------
    wger_base_url: HttpUrl

    # ---------- inbound auth strategy ----------
    mcp_auth: AuthStrategy = AuthStrategy.oidc

    # ---- SSO identity provider (OIDC) ----
    # Realm/tenant issuer, e.g. https://idp.example.com/realms/main (Keycloak)
    # or https://tenant.auth0.com/. The same IdP wger uses for OIDC login.
    oidc_issuer: HttpUrl | None = None
    # Resolved from the discovery document when omitted.
    oidc_jwks_uri: HttpUrl | None = None
    oidc_token_endpoint: HttpUrl | None = None
    oidc_authorization_endpoint: HttpUrl | None = None

    # Inbound-token validation.
    mcp_oidc_audience: str | None = None  # if set, inbound 'aud'/'azp' must contain it
    mcp_oidc_algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    mcp_oidc_username_claim: str = "preferred_username"
    mcp_oidc_allowed_users: list[str] = Field(default_factory=list)
    mcp_jwks_ttl_seconds: int = 3600

    # ---- token exchange (this server as a confidential OIDC client) ----
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    # Target audience of the exchange = wger's OIDC client id at the IdP.
    wger_oidc_audience: str | None = None

    # ---- wger allauth headless exchange ----
    # The allauth provider id of the OIDC connection configured in wger
    # (SocialApp.provider_id). For a generic OIDC connection this is the slug
    # set in wger's admin; it is often — but not always — "openid_connect".
    # The exchange requests an access_token aud'd at wger so verify_token accepts it.
    wger_allauth_provider: str = "openid_connect"
    wger_allauth_provider_token_path: str = "/allauth/app/v1/auth/provider/token"

    # ---- single-user strategies (MCP_AUTH=static_token | none) ----
    # A personal wger DRF API key, sent as 'Authorization: Token <...>'.
    wger_dev_token: str | None = None
    # Shared secret callers must present as a bearer token under
    # MCP_AUTH=static_token. Unused by the other strategies.
    mcp_static_token: str | None = None

    # ---------- transport ----------
    host: str = "0.0.0.0"
    port: int = 8765
    mcp_path: str = "/mcp"
    # Externally reachable base URL of this server, used as the OAuth
    # protected-resource identifier and in metadata. Falls back to host:port.
    mcp_public_url: HttpUrl | None = None

    # ---- AS-facade endpoint paths ----
    # This origin acts as the OAuth authorization server for the client. Many
    # MCP clients (e.g. claude.ai) assume the conventional root paths and ignore
    # the custom authorization_endpoint advertised in the AS metadata, so default
    # to /authorize + /token. Override (e.g. /oauth/authorize) if a client wants
    # a different path — no rebuild needed, just set the env var.
    oauth_authorize_path: str = "/authorize"
    oauth_token_path: str = "/token"

    # DNS rebinding protection. Empty list disables the check.
    allowed_hosts: list[str] = Field(default_factory=list)

    # ---------- tool surface ----------
    # Tool groups to register, by module name (see wger_mcp.tools.TOOL_GROUPS).
    # Empty = every group. Useful for agents driven by small local models, which
    # lose accuracy as the tool count grows, and for single-purpose agents that
    # need only part of the API. An unknown name is rejected at startup.
    mcp_tools: list[str] = Field(default_factory=list)

    # ---------- localisation ----------
    # Default ISO 639-1 language for content lookups. Used as the default for
    # the exercise-search tools' ``language`` argument and to pick which
    # localised Open Food Facts fields (``product_name_<lang>``,
    # ``ingredients_text_<lang>``) are requested and preferred. Per-call
    # arguments always win over this default.
    default_language: str = "en"

    @field_validator("mcp_oidc_algorithms", mode="after")
    @classmethod
    def _normalize_algs(cls, v: list[str]) -> list[str]:
        return [a.strip().upper() for a in v if a.strip()]

    @field_validator("oauth_authorize_path", "oauth_token_path", mode="after")
    @classmethod
    def _ensure_leading_slash(cls, v: str) -> str:
        v = v.strip()
        return v if v.startswith("/") else "/" + v

    @field_validator("mcp_tools", mode="after")
    @classmethod
    def _normalize_tools(cls, v: list[str]) -> list[str]:
        return [t.strip().lower() for t in v if t.strip()]

    @field_validator("default_language", mode="after")
    @classmethod
    def _normalize_language(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.fullmatch(r"[a-z]{2}", v):
            raise ValueError(
                f"DEFAULT_LANGUAGE must be a two-letter ISO 639-1 code, got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _check_strategy_requirements(self) -> Settings:
        if self.mcp_auth is AuthStrategy.oidc:
            missing = [
                name
                for name, val in (
                    ("OIDC_ISSUER", self.oidc_issuer),
                    ("OIDC_CLIENT_ID", self.oidc_client_id),
                    ("OIDC_CLIENT_SECRET", self.oidc_client_secret),
                    ("WGER_OIDC_AUDIENCE", self.wger_oidc_audience),
                )
                if not val
            ]
            if missing:
                raise ValueError("MCP_AUTH=oidc requires: " + ", ".join(missing))
        elif self.mcp_auth is AuthStrategy.static_token:
            missing = [
                name
                for name, val in (
                    ("MCP_STATIC_TOKEN", self.mcp_static_token),
                    ("WGER_DEV_TOKEN", self.wger_dev_token),
                )
                if not val
            ]
            if missing:
                raise ValueError("MCP_AUTH=static_token requires: " + ", ".join(missing))
            # The secret is the only thing standing between the network and full
            # access to the wger account, so refuse trivially guessable values.
            if len(str(self.mcp_static_token)) < MIN_STATIC_TOKEN_LENGTH:
                raise ValueError(
                    f"MCP_STATIC_TOKEN must be at least {MIN_STATIC_TOKEN_LENGTH} "
                    "characters; generate one with: openssl rand -hex 32"
                )
        elif self.mcp_auth is AuthStrategy.none and not self.wger_dev_token:
            raise ValueError("MCP_AUTH=none requires WGER_DEV_TOKEN (a wger DRF API key)")
        return self

    # ---------- derived ----------
    @property
    def wger_api_root(self) -> str:
        return str(self.wger_base_url).rstrip("/") + "/api/v2"

    @property
    def provider_token_url(self) -> str:
        return str(self.wger_base_url).rstrip("/") + self.wger_allauth_provider_token_path


def _csv_to_json_list(name: str) -> None:
    """Allow comma-separated values for list-typed env vars."""
    import os

    if name not in os.environ:
        return
    raw = os.environ[name].strip()
    if not raw:
        os.environ[name] = "[]"
        return
    if raw.startswith("["):
        return
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    os.environ[name] = "[" + ",".join(f'"{p}"' for p in parts) + "]"


_CSV_VARS = (
    "MCP_OIDC_ALGORITHMS",
    "MCP_OIDC_ALLOWED_USERS",
    "ALLOWED_HOSTS",
    "MCP_TOOLS",
)


def load_settings() -> Settings:
    for var in _CSV_VARS:
        _csv_to_json_list(var)
    return Settings()  # type: ignore[call-arg]
