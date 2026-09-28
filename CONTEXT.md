# Context — wger MCP server

Glossary of the ubiquitous language for this project. Definitions only — no
implementation details. See `docs/adr/` for decisions.

## Terms

### Inbound auth

How an MCP *client* (Claude Desktop, a script, …) proves its identity to the
MCP server. Selectable via `MCP_AUTH` (`wger_oidc` | `oidc` | `static_token` |
`none`). Gates every `/mcp/*` request except the public paths (`/health`,
`/.well-known/*`, and the [[AS facade]] endpoints).

### Outbound auth

How the MCP server proves identity to the upstream **wger** REST API. Per
request under both multi-user strategies, as the specific [[wger identity]]
behind the inbound credential: under `wger_oidc` the inbound token itself
([[Pass-through]]), under `oidc` a [[wger JWT]] obtained by [[Token exchange]].
Under the single-user strategies it is a static DRF API key (`WGER_API_KEY`)
shared by every request. The username/password web-form session is **removed**.

### wger identity

The wger user account whose data an operation reads or writes. Under
`wger_oidc` and `oidc` it varies per request and follows from the inbound
credential; under the single-user strategies it is fixed (the owner of
`WGER_API_KEY`).

### Single-user vs multi-user

- **Multi-user:** each client maps to its own wger account; the MCP performs
  every operation as that specific wger identity. *(`MCP_AUTH=wger_oidc`, where
  wger itself is the provider — recommended on wger >= 2.7; or `MCP_AUTH=oidc`,
  the built-in default, which requires an [[IdP]].)*
- **Single-user:** the whole MCP server acts as one wger account, via a static
  API key and no IdP. Two variants differing only in whether inbound requests
  are authenticated: `static_token` validates a shared secret and is safe to
  expose over TLS; `none` performs no inbound authentication at all and is
  localhost-only. *(Re-introduced 2026-08-12 for self-hosting; the earlier
  removal on 2026-06-18 left no IdP-free option that was safe to expose.)*

### Pass-through

The model where the inbound credential **is** the outbound credential: a
wger-issued token presented by the client is forwarded by the MCP to wger
unchanged, so no per-user secrets are stored server-side and nothing is
translated. *(`MCP_AUTH=wger_oidc`. Possible since wger 2.7 made wger its own
OAuth2/OIDC provider whose API accepts the tokens it issues — see
[ADR 0005](docs/adr/0005-native-wger-oidc.md). Before that, [[Token exchange]]
was the only option.)*

### IdP (identity provider)

The external single sign-on authority both wger and the MCP trust — **any OIDC
provider** (Keycloak, Authentik, Auth0, Okta, …); endpoints are taken from its
discovery document, so the MCP is not provider-locked. wger must be wired to the
same IdP as an OIDC social-login provider; the MCP validates the same
IdP-issued tokens. Required only by `MCP_AUTH=oidc`: under `wger_oidc` wger is
itself the provider, and the single-user strategies have none.

### Token exchange

Turning a verified [[IdP]] token into a **native wger credential**, because in
wger 2.6 the REST API accepts only wger-native tokens (DRF `Token`, wger-issued
JWT, or session) — never a foreign IdP token. Used by `MCP_AUTH=oidc`. Two
steps:

1. The MCP is a **confidential OIDC client** and uses RFC 8693 to exchange the
   inbound token for an **access_token** whose `aud` is wger's OIDC client.
2. The MCP posts that token (under `token.id_token`) to wger's allauth headless
   `/allauth/app/v1/auth/provider/token`, and wger returns a [[wger JWT]].
   Requires the user to have **no wger-side MFA** (MFA delegated to the IdP).

### wger JWT

A wger-issued, RS256, `Authorization: Bearer` token accepted by the wger REST
API. Two flavours, both Bearer: allauth-headless JWT (from the exchange) and
SimpleJWT. Access token lives ~5 min; refresh ~120 days and **rotates**
(single-use, blacklist-after-rotation).

### AS facade

The server presenting **itself** as the OAuth authorization server while
bridging to the real provider — the [[IdP]] under `oidc`, wger under
`wger_oidc`. For clients that treat the MCP origin as the AS (e.g. claude.ai)
and cannot reach a private provider directly: it serves AS metadata, `302`s
`/authorize` to the provider (front-channel), and reverse-proxies `/token` and
`/register` (back-channel). Those paths are the defaults clients assume
(override via `OAUTH_AUTHORIZE_PATH` / `OAUTH_TOKEN_PATH` /
`OAUTH_REGISTER_PATH`; switch the whole facade off with `MCP_AS_FACADE=false`).
The provider still mints the tokens; the facade only relays — except for the
[[API scopes]], which it adds under `wger_oidc`. See `docs/adr/0003-*.md` and
`docs/adr/0005-*.md`.

### API scopes

What a wger 2.7 access token is allowed to do: `api:read` for safe methods,
`api:write` for everything else, checked on every API call. Requested by this
server as `MCP_WGER_SCOPES` (plus `openid` for the identity) and named on wger's
consent screen, which is the one moment a user sees that an assistant is being
granted write access to their training, nutrition and body data.
