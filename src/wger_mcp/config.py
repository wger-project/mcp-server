"""Settings for wger-mcp.

``MCP_TRANSPORT`` picks how clients reach the server, and that decides which of
the settings below matter:

- ``stdio`` — the client spawns this process locally. No listener, no inbound
  auth; the only credential is ``WGER_API_KEY``. Everything under *inbound auth
  strategy* and *transport* is ignored.
- ``http`` (default) — streamable HTTP, with the multi-user auth described
  below.

Since wger 2.6, the server is **multi-user**: every request acts as the
caller's own wger account. Two strategies do that, differing in who issues the
token:

``MCP_AUTH=wger_oidc`` — **wger itself** (2.7+) is the OAuth2/OIDC provider and
its REST API accepts the access tokens it issues. This server is then a plain
resource server: the caller's wger token is put on the outbound ``/api/v2/``
call unchanged. No IdP, no token exchange, no client credentials — only
``WGER_BASE_URL``. See ``docs/adr/0005-native-wger-oidc.md``.

``MCP_AUTH=oidc`` — an **external** IdP (Keycloak, Authentik, Auth0, Okta, …)
issues the token, for setups already fronted by one and for wger < 2.7:

- **Inbound** — the client presents an OIDC-issued token (via MCP-native OAuth
  or out-of-band). The server validates it against the IdP's JWKS.
- **Outbound** — the server is a *confidential* OIDC client. It exchanges the
  inbound token (RFC 8693) for one whose audience is wger's OIDC client, posts
  that to wger's allauth headless ``provider/token`` endpoint, and uses the
  returned wger JWT as ``Authorization: Bearer`` on the wger REST API. See
  ``docs/adr/0001-multi-user-auth-via-oidc-token-exchange.md``.

Endpoints (JWKS, token, authorization) are resolved from the provider's
discovery document (``{issuer}/.well-known/openid-configuration``) unless
overridden — the issuer being ``WGER_BASE_URL`` under ``wger_oidc`` and
``OIDC_ISSUER`` under ``oidc``.

Two single-user strategies avoid the IdP entirely, both calling wger with a
static ``WGER_API_KEY`` (a personal DRF API key):

- ``MCP_AUTH=static_token`` — callers must present ``MCP_STATIC_TOKEN`` as a
  bearer token. Inbound requests *are* authenticated, so this is safe to expose
  over TLS; the secret grants full access to that one wger account.
- ``MCP_AUTH=none`` — no inbound authentication at all. Anyone who can reach
  ``/mcp`` acts as the account behind that key, so bind it to localhost.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, HttpUrl, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Minimum length for MCP_STATIC_TOKEN. 32 chars is roughly the shortest value
# that is still awkward to brute-force; `openssl rand -hex 32` gives 64.
MIN_STATIC_TOKEN_LENGTH = 32


class AuthStrategy(StrEnum):
    #: wger 2.7+ is itself the OAuth2/OIDC provider; its token is passed through.
    wger_oidc = "wger_oidc"
    #: An external OIDC IdP issues the token; it is exchanged for a wger one.
    oidc = "oidc"
    static_token = "static_token"
    none = "none"


class Transport(StrEnum):
    """How the MCP client reaches this server.

    ``stdio`` — the client spawns this process and speaks JSON-RPC over
    stdin/stdout. Local and single-user: there is no listener and no inbound
    authentication, since the client *is* the parent process. Only the outbound
    wger credential (``WGER_API_KEY``) is configured.

    ``http`` — streamable HTTP on ``HOST``/``PORT``, with one of the inbound
    ``MCP_AUTH`` strategies. The multi-user deployment.
    """

    stdio = "stdio"
    http = "http"


#: Settings file read under ``http``. The single place this path is spelled out.
DEFAULT_ENV_FILE = ".env"


def transport_in_environ(environ: Mapping[str, str] | None = None) -> Transport | None:
    """The transport the environment asks for, or ``None`` when it says nothing.

    Separate from the default so callers can tell "unset" from "set to http" —
    a caller that reports on an overridden value must not report on one nobody
    set. The lookup is deliberately case-insensitive: pydantic-settings matches
    env vars that way, and a case-sensitive one here would let
    ``mcp_transport=stdio`` resolve to one transport and validate as the other.
    """
    env = os.environ if environ is None else environ
    for key, value in env.items():
        if key.lower() == "mcp_transport" and value.strip():
            raw = value.strip().lower()
            if raw not in set(Transport):
                raise ValueError(
                    f"MCP_TRANSPORT must be one of {', '.join(Transport)}, got {value!r}"
                )
            return Transport(raw)
    return None


def resolve_transport(
    cli_value: str | None = None, environ: Mapping[str, str] | None = None
) -> Transport:
    """Decide the transport *before* any settings are loaded.

    This one setting cannot come from an env file, because it is what decides
    whether an env file is read at all. So it is resolved from the command line
    first, then the process environment, and defaults to ``http``.
    """
    if cli_value:
        return Transport(cli_value)
    return transport_in_environ(environ) or Transport.http


_TRANSPORT_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?mcp_transport\s*=\s*['\"]?([A-Za-z]+)",
    re.IGNORECASE | re.MULTILINE,
)


def transport_declared_in(env_file: str | None) -> str | None:
    """The transport an env file tries to set, if it does.

    Used only to turn a setting that cannot take effect into a readable error:
    callers pass the resolved transport as an override, so a line this pattern
    misses costs a better message, never correct behaviour.
    """
    if not env_file:
        return None
    try:
        text = Path(env_file).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _TRANSPORT_ASSIGNMENT.search(text)
    return match.group(1).lower() if match else None


def env_file_for(transport: Transport, override: str | None = None) -> str | None:
    """The settings file to read for ``transport`` — the whole policy, in one place.

    ``None`` under stdio: the working directory belongs to whichever MCP client
    spawned the process, so a file sitting there is not the user's configuration
    and must not be treated as such. An explicit ``override`` always wins.

    That override must exist. pydantic-settings skips a missing dotenv file
    without a word, which is right for the optional default but wrong for a path
    someone typed: a typo would otherwise start the server on whatever the
    environment still holds — a different wger instance, a different account.
    """
    if override is not None:
        if not Path(override).is_file():
            raise FileNotFoundError(
                f"env file not found: {override}. It was given explicitly, so it is "
                "not treated as optional — fix the path or drop the option to fall "
                f"back to {DEFAULT_ENV_FILE} (http) or the environment alone (stdio)."
            )
        return override
    return None if transport is Transport.stdio else DEFAULT_ENV_FILE


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # No env_file default: which file to read (if any) is the transport's
        # decision, made once in env_file_for() and passed by load_settings().
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="",
        # Fields with a validation_alias (wger_dev_token) are otherwise only
        # settable through that alias, and the env source keys by field name.
        populate_by_name=True,
    )

    # ---------- upstream wger ----------
    wger_base_url: HttpUrl

    # ---------- inbound auth strategy ----------
    mcp_auth: AuthStrategy = AuthStrategy.oidc

    # ---- native wger OIDC (MCP_AUTH=wger_oidc) ----
    # Scopes this server asks wger for. `openid` identifies the user; roughly
    # half the tool surface writes, hence both API scopes. A read-only
    # deployment drops `api:write` and pairs that with a read-only MCP_TOOLS.
    mcp_wger_scopes: list[str] = Field(
        default_factory=lambda: ["openid", "api:read", "api:write"]
    )
    # Whether this origin presents itself as the authorization server
    # (auth/asfacade.py). On by default because claude.ai and others ignore the
    # `authorization_servers` pointer in the protected-resource metadata; turn
    # it off to send clients straight to wger, which is the honest answer for
    # clients that do follow the pointer.
    mcp_as_facade: bool = True

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

    # ---- single-user strategies (MCP_AUTH=static_token | none) and stdio ----
    # A personal wger DRF API key, sent as 'Authorization: Token <...>'.
    # WGER_API_KEY is the name to document — it says what the value is. The
    # older WGER_DEV_TOKEN keeps working for existing deployments.
    wger_dev_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("WGER_API_KEY", "WGER_DEV_TOKEN"),
    )
    # Shared secret callers must present as a bearer token under
    # MCP_AUTH=static_token. Unused by the other strategies.
    mcp_static_token: str | None = None

    # ---------- transport ----------
    # Everything below this line except `mcp_transport` applies to http only.
    mcp_transport: Transport = Transport.http

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
    # Dynamic client registration (RFC 7591), proxied only when the provider
    # offers it — it publishes a registration_endpoint exactly then.
    oauth_register_path: str = "/register"

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

    @field_validator(
        "oauth_authorize_path", "oauth_token_path", "oauth_register_path", mode="after"
    )
    @classmethod
    def _ensure_leading_slash(cls, v: str) -> str:
        v = v.strip()
        return v if v.startswith("/") else "/" + v

    @field_validator("mcp_wger_scopes", mode="after")
    @classmethod
    def _normalize_scopes(cls, v: list[str]) -> list[str]:
        # A space-separated string is what OAuth itself uses, so accept it too:
        # MCP_WGER_SCOPES="openid api:read" is the spelling people will reach for.
        out: list[str] = []
        for entry in v:
            out.extend(part for part in entry.split() if part)
        return list(dict.fromkeys(out))

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
        if self.mcp_transport is Transport.stdio:
            return self._check_stdio_requirements()
        if self.mcp_auth is AuthStrategy.wger_oidc:
            # Nothing to check beyond WGER_BASE_URL, which every strategy needs:
            # wger issues the token, validates it and names the scopes, so this
            # server holds no client credentials and no audience of its own.
            # Listed explicitly so the absence is a decision, not an oversight.
            if not self.mcp_wger_scopes:
                raise ValueError("MCP_WGER_SCOPES must not be empty")
        elif self.mcp_auth is AuthStrategy.oidc:
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
                    ("WGER_API_KEY", self.wger_dev_token),
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
            raise ValueError("MCP_AUTH=none requires WGER_API_KEY (a wger DRF API key)")
        return self

    def _check_stdio_requirements(self) -> Settings:
        """Validate the stdio setup, where inbound auth does not exist.

        The MCP client spawns this process and owns both ends of the pipe, so
        there is nothing to authenticate: whoever can talk to us already runs as
        the user. Only the outbound wger credential is required.
        """
        chosen = "mcp_auth" in self.model_fields_set
        if chosen and self.mcp_auth is not AuthStrategy.none:
            # Every strategy, not just oidc: quietly rewriting a chosen one would
            # also skip the checks that come with it — MCP_AUTH=static_token used
            # to pass here with a token http rejects as too short to be a secret.
            raise ValueError(
                f"MCP_TRANSPORT=stdio cannot use MCP_AUTH={self.mcp_auth.value}: "
                "inbound authentication does not exist there, since the client "
                "spawned this process and owns both ends of the pipe. Drop MCP_AUTH "
                "and set WGER_API_KEY, or run with MCP_TRANSPORT=http."
            )
        # Only ever fills in the default, never overrides a choice — the branch
        # above has already refused those. It says that nothing gates the pipe,
        # and makes build_token_provider() use the static key outbound.
        self.mcp_auth = AuthStrategy.none
        if not self.wger_dev_token:
            raise ValueError(
                "MCP_TRANSPORT=stdio requires WGER_API_KEY, a wger API key from "
                "your wger profile (Settings → API key)"
            )
        return self

    # ---------- derived ----------
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
    "MCP_WGER_SCOPES",
    "ALLOWED_HOSTS",
    "MCP_TOOLS",
)


def load_settings(*, env_file: str | None = DEFAULT_ENV_FILE, **overrides: Any) -> Settings:
    """Build :class:`Settings` from the environment.

    ``env_file`` is resolved relative to the current working directory; callers
    that know the transport should get it from :func:`env_file_for` rather than
    deciding again. ``overrides`` take precedence over both the file and the
    environment; ``server.main`` uses them for command-line flags.
    """
    for var in _CSV_VARS:
        _csv_to_json_list(var)
    return Settings(_env_file=env_file, **overrides)  # type: ignore[call-arg]
