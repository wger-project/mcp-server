# Native wger OIDC: pass the token through

**Status:** accepted (2026-08-28)

Supersedes the *transport* half of
[0001](0001-multi-user-auth-via-oidc-token-exchange.md) for wger >= 2.7. 0001
stays as the record of why the exchange existed, and remains the mode for
wger 2.6 and for deployments already fronted by an SSO provider.

## Context

0001's whole machinery exists for one reason: wger's REST API accepted only
wger-native credentials, so a token from an identity provider had to be traded
for one. Two hops, a confidential OIDC client, an RFC 8693 exchange and an
allauth headless login — all to answer "what does this user's wger credential
look like".

wger 2.7 removes the premise. It ships `allauth.idp.oidc`: wger is an
OAuth2/OIDC provider *and* its API accepts the access tokens it issues
(`OidcTokenAuthentication` in `DEFAULT_AUTHENTICATION_CLASSES`, with `api:read`
gating safe methods and `api:write` everything else). There is nothing left to
broker.

Three things fall away with the exchange, and the third is what makes a public
deployment possible at all:

- **The third-party IdP.** The single biggest obstacle both for a public
  mcp.wger.de and for self-hosters, who were told to stand up Keycloak to use a
  fitness tracker.
- **The two hops** and the credentials they needed (`OIDC_CLIENT_ID`,
  `OIDC_CLIENT_SECRET`, `WGER_OIDC_AUDIENCE`, the allauth provider slug).
- **The wger-side MFA blocker.** 2.6's headless `provider/token` refuses to log
  in a user who has enrolled a TOTP/WebAuthn authenticator *in wger*, with no
  setting to skip it — so 0001's model forced users to leave wger-side 2FA off.
  Here the authorization-code flow runs in the user's browser through wger's
  ordinary login page, so whatever MFA they enrolled simply applies.

## Decision

Add a fourth inbound strategy, `MCP_AUTH=wger_oidc`, in which this server is a
plain resource server: it takes the caller's wger access token and puts it on
the outbound `/api/v2/` call unchanged.

### The token is not validated here

wger's access tokens are **opaque** — allauth's default format, which wger does
not override — so there is nothing to verify against a JWKS, and the existing
`oidc` validation path does not apply. Introspection is off by default in
allauth, and turning it on would buy a round trip per request for an answer wger
gives anyway on the call itself.

wger is therefore the single authority on whether a token is live and which
scopes it carries. The price is that a bad token is only noticed at the first
API call, which is why the error mapping below is part of this decision rather
than a later polish item.

### The AS facade stays, pointed at wger

[0003](0003-oauth-authorization-server-facade.md)'s original justification — a
private IdP — is gone, but the client limitation that drove it is not: claude.ai
treats the MCP origin as the authorization server and ignores the
`authorization_servers` pointer. The facade code was already generic, so it only
needed different endpoints, discovered from `WGER_BASE_URL`.

It gained two things, both because a generic MCP client cannot know that wger's
API is gated behind `api:read`/`api:write`:

- **`/register` is proxied** when wger offers dynamic client registration. wger
  publishes a `registration_endpoint` in its discovery document exactly when DCR
  is enabled, so this needs no configuration on the MCP side.
- **The API scopes are added** to the proxied registration and to the
  `/authorize` query, on top of what the client asked for. Without this a client
  registers with `openid` alone, its later `/authorize` is refused with
  `invalid_scope` (allauth requires the requested scopes to be a subset of the
  client's), and the connector is dead with no diagnosable error. This server
  knows which scopes it needs and is the party the client believes it is
  registering with, so it is the right place to add them. Nothing is hidden from
  the user by that: what the facade adds is what the consent screen names.

### Identity is resolved lazily, keyed by a fingerprint

With opaque tokens the middleware cannot know who is calling. Nothing in the
request path needs the username except logging and the optional allowlist, so it
is fetched on first use per token — from `/api/v2/userprofile/`, which needs
only `api:read`, rather than from OIDC `userinfo`, which would need the
`profile` scope this server does not request — and cached in memory. The cache
key and the identity's subject are a SHA-256 prefix of the token; the raw token
is never logged and never used as a key.

### 401 and 403 are mapped, not passed through raw

A model that receives a bare `401` retries, and the retry fails identically:
the token is the caller's own, and only a new authorization can produce a live
one. So `api_err` attaches a hint saying the connection must be authorized
again. For a `403`, wger's body names the missing scope, and the hint repeats
it — a user who granted only `api:read` would otherwise watch every write tool
fail opaquely.

### `oidc` stays; the default does not change

`oidc` remains for wger < 2.7 and for deployments already fronted by
Keycloak/Authentik, and `static_token` for single-user self-hosting. The
built-in default stays `oidc`: an existing deployment has `OIDC_*` configured
and `MCP_AUTH` possibly unset, and flipping the default would silently turn it
into a pass-through server whose tokens wger rejects. `wger_oidc` is the
documented recommendation instead, set explicitly.

## Considered options

- **Introspection, or JWT-format access tokens.** Would let a bad token be
  rejected at the door rather than at the first API call. Costs a round trip per
  request (introspection) or a wger-side format change plus key distribution
  (JWT), to improve an error path that is already reported clearly. Revisit if
  early rejection ever matters more.
- **Replace the exchange rather than add a strategy.** Cleaner, one code path
  less. Refused: it would strand wger 2.6 deployments and everyone whose users
  authenticate through a corporate SSO.
- **Drop the AS facade and advertise wger directly.** Spec-correct, one hop
  shorter — and broken for claude.ai. Available as `MCP_AS_FACADE=false` for
  deployments whose clients all follow the pointer.
- **Ask wger for `profile` as well, and read the username from `userinfo`.**
  The textbook way to name the caller, but it widens the grant for something the
  server needs only when an allowlist is configured.

## Consequences

- A wger >= 2.7 deployment is `MCP_AUTH=wger_oidc` plus `WGER_BASE_URL`. No
  identity provider, no client credentials, no audience, no provider slug.
- MFA enrolled in wger works, which 0001's mode could not do.
- **Revocation has a gap on the wger side.** allauth ships no "connected
  applications" page: a user can grant an assistant write access to every
  training, nutrition and body record and has no way to take it back short of
  the Django admin. That is a wger-side item, tracked in `docs/HANDOFF.md`.
- **Token lifetimes are generous** for a credential a third-party assistant
  holds: 1 h access, 120 d refresh with rotation. Worth revisiting per client.
- Every request costs wger a token lookup, and the fan-out tools issue many
  requests in parallel. Watch wger's throttle counters; the semaphore caps in
  the tool modules are the lever on this side.
- This server still stores nothing: no database, no per-user secrets. The only
  cache is username-by-fingerprint, in memory, per process.
