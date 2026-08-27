"""OAuth 2.0 Authorization Server facade (RFC 8414) fronting an external IdP.

claude.ai's MCP connector treats the MCP server's own origin as the
authorization server: it discovers ``{origin}/.well-known/oauth-authorization-server``
and drives ``/authorize`` + ``/token`` against that same origin. It does **not**
follow the ``authorization_servers`` pointer to a different host.

So even where the real provider *is* publicly reachable — wger itself, under
``MCP_AUTH=wger_oidc`` — the client still has to find the flow on this origin.
And where it is not (a Keycloak on a private network behind a tunnelled MCP
server), the facade is what makes the flow possible at all. Either way it is
thin:

- advertise *this* origin as the authorization server,
- ``/authorize`` → 302 the browser to the provider's authorization endpoint
  (the user's browser reaches it directly; cookies, login and MFA stay there),
- ``/token`` → reverse-proxy the back-channel token request,
- ``/register`` → likewise, when the provider offers dynamic client
  registration.

(Paths default to the conventional ``/authorize`` / ``/token``; override with
``OAUTH_AUTHORIZE_PATH`` / ``OAUTH_TOKEN_PATH``.)

The provider never has to be publicly reachable; the client only ever talks to
this origin. Tokens are still minted by the provider, so nothing about how they
are validated changes.

**Scopes.** A generic MCP client has no way of knowing that wger gates its API
behind ``api:read``/``api:write``: it will register, and ask, for ``openid``
alone, and every later API call then fails with ``insufficient_scope`` — or the
``/authorize`` itself is refused, since allauth requires the requested scopes to
be a subset of the client's. This server *does* know which scopes it needs, and
it is the party the client believes it is registering with, so it adds them to
both the proxied registration and the authorization request. Nothing is
withheld from the user by that: the scopes it adds are the ones the consent
screen then names.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import parse_qsl, urlencode

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

log = logging.getLogger(__name__)

# Default facade endpoint paths. Conventional root paths because clients like
# claude.ai assume them and ignore the authorization_endpoint in the AS metadata.
# Overridable per-deployment via OAUTH_AUTHORIZE_PATH / OAUTH_TOKEN_PATH (config).
AUTHORIZE_PATH = "/authorize"
TOKEN_PATH = "/token"
REGISTER_PATH = "/register"
AS_METADATA_PATH = "/.well-known/oauth-authorization-server"

#: Identity scopes any OIDC provider offers. Only used when the deployment does
#: not name its own — the external-IdP mode, where this server never had a say
#: in which scopes the client asks for.
_DEFAULT_SCOPES = ["openid", "profile", "email", "offline_access"]


def _merge_scopes(requested: str | None, required: list[str]) -> str:
    """The client's scope string with anything missing from ``required`` appended.

    Order matters only for readability: what the client asked for comes first,
    so the addition is visible as an addition. Duplicates are dropped because
    allauth compares sets and a repeated scope reads like a bug.
    """
    scopes = list(dict.fromkeys((requested or "").split()))
    scopes += [s for s in required if s not in scopes]
    return " ".join(scopes)


class AuthorizationServerFacade:
    """Presents this origin as an OAuth AS, bridging to an external IdP."""

    def __init__(
        self,
        *,
        idp_authorization_endpoint: str,
        idp_token_endpoint: str,
        idp_registration_endpoint: str | None = None,
        authorize_path: str = AUTHORIZE_PATH,
        token_path: str = TOKEN_PATH,
        register_path: str = REGISTER_PATH,
        required_scopes: list[str] | None = None,
        advertised_scopes: list[str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._idp_authorize = idp_authorization_endpoint
        self._idp_token = idp_token_endpoint
        self._idp_register = idp_registration_endpoint
        self._authorize_path = authorize_path
        self._token_path = token_path
        self._register_path = register_path
        # Empty in external-IdP mode: there this server does not know what wger
        # was told to accept, so it forwards whatever the client asked for.
        self._required_scopes = required_scopes or []
        self._advertised_scopes = advertised_scopes or _DEFAULT_SCOPES
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def supports_registration(self) -> bool:
        """Whether the provider offers DCR — it publishes ``registration_endpoint``
        in its discovery document exactly then, so there is nothing to configure."""
        return self._idp_register is not None

    async def aclose(self) -> None:
        await self._client.aclose()

    def metadata(self, origin: str) -> dict:
        """RFC 8414 metadata advertising this origin's facade endpoints."""
        base = origin.rstrip("/")
        meta = {
            "issuer": base,
            "authorization_endpoint": base + self._authorize_path,
            "token_endpoint": base + self._token_path,
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
            ],
            "scopes_supported": list(self._advertised_scopes),
        }
        if self._idp_register is not None:
            meta["registration_endpoint"] = base + self._register_path
        return meta

    async def authorize(self, request: Request) -> Response:
        """Front-channel: bounce the browser to the provider, query string intact.

        Everything the provider needs (client_id, redirect_uri, PKCE challenge,
        state, scope) is in the query and is preserved verbatim, so it validates
        against the real client and redirects straight back to the client's
        registered redirect_uri afterwards.

        The one thing that may be rewritten is ``scope``, and only to *add* the
        scopes this server cannot work without (see the module docstring). The
        query is left byte-for-byte alone whenever they are already there, which
        is every request in external-IdP mode.
        """
        qs = request.url.query
        if self._required_scopes:
            params = parse_qsl(qs, keep_blank_values=True)
            scope = next((v for k, v in params if k == "scope"), None)
            merged = _merge_scopes(scope, self._required_scopes)
            if merged != scope:
                log.debug("authorize: scope %r → %r", scope, merged)
                params = [(k, v) for k, v in params if k != "scope"]
                params.append(("scope", merged))
                qs = urlencode(params)
        target = self._idp_authorize + (f"?{qs}" if qs else "")
        return RedirectResponse(target, status_code=302)

    async def token(self, request: Request) -> Response:
        """Back-channel: reverse-proxy the token request to the provider verbatim.

        Forwards the urlencoded body plus the content-type, the client's
        Authorization header (client_secret_basic) and Accept, then returns the
        response unchanged. Client authentication is the provider's job.
        """
        body = await request.body()
        return await self._proxy(self._idp_token, request, body, what="token")

    async def register(self, request: Request) -> Response:
        """Back-channel: reverse-proxy dynamic client registration (RFC 7591).

        Registering against this origin rather than the provider's is what keeps
        the client talking to one host throughout, and it is also the only place
        the API scopes can be added — a client that registers with ``openid``
        alone is refused at ``/authorize`` later, and the connector is then dead
        with no useful error anywhere.

        A body that is not JSON is forwarded untouched: it is the provider's
        business to reject it, and guessing at a shape we do not understand is
        how a proxy turns a clear 400 into a confusing one.
        """
        body = await request.body()
        if self._required_scopes:
            body = self._with_registration_scopes(body)
        return await self._proxy(self._idp_register, request, body, what="registration")

    def _with_registration_scopes(self, body: bytes) -> bytes:
        try:
            payload = json.loads(body)
        except ValueError:
            return body
        if not isinstance(payload, dict):
            return body
        requested = payload.get("scope")
        if isinstance(requested, list):
            requested = " ".join(str(s) for s in requested)
        elif not isinstance(requested, str):
            requested = None
        merged = _merge_scopes(requested, self._required_scopes)
        if merged == requested:
            return body
        log.debug("register: scope %r → %r", requested, merged)
        payload["scope"] = merged
        return json.dumps(payload).encode("utf-8")

    async def _proxy(
        self, endpoint: str | None, request: Request, body: bytes, *, what: str
    ) -> Response:
        if endpoint is None:  # pragma: no cover - the route is not registered
            return JSONResponse({"error": "not_supported"}, status_code=404)
        headers = {}
        for h in ("content-type", "authorization", "accept"):
            v = request.headers.get(h)
            if v:
                headers[h] = v
        try:
            resp = await self._client.post(endpoint, content=body, headers=headers)
        except httpx.HTTPError as exc:
            log.warning("%s-endpoint proxy failed: %s", what, exc)
            return JSONResponse(
                {"error": "temporarily_unavailable", "error_description": str(exc)},
                status_code=502,
            )
        out_headers = {}
        ct = resp.headers.get("content-type")
        if ct:
            out_headers["content-type"] = ct
        return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)
