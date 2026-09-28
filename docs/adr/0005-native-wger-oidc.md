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

### The token is checked by asking wger, and cached briefly

wger's access tokens are **opaque** — allauth's default format, which wger does
not override — so there is nothing to verify against a JWKS, and the existing
`oidc` validation path does not apply. The middleware asks
`/api/v2/userprofile/` instead and caches a positive answer for 60 seconds.

*Amended 2026-09-28.* The first version did not check the token at all and let
wger refuse it on the API call. That refusal reaches the client as a tool result
inside an HTTP 200, and MCP clients refresh a token only on an HTTP 401: once
the one-hour access token expired, every call failed until the user reconnected
by hand. Checking at the door costs one lookup per token and minute; the TTL
bounds how late a revoked token is noticed, and calls in that window still get
wger's refusal with a re-authorize hint. When wger cannot be reached the answer
is `503`, not `401`: a 401 would make the client discard a token that may be
fine.

Since anyone can send a bearer, the check is also an upstream request anyone can
trigger. Three things bound it: at most 32 lookups run against wger at once
(past that the answer is `503` rather than another lookup — a lookup outlives a
client that hangs up, so open connections alone would not bound it), a refusal
is remembered for 10 seconds so a token sent again and again costs one lookup,
and the lookups share one pooled client, closed at shutdown. Rate limiting by
source stays the reverse proxy's job.

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
- **wger's client authentication methods are advertised**, `none` included, as
  read from its discovery document. Public PKCE clients registering through
  DCR need `none`; the external-IdP list offered only secret-based methods.
- **`resource` is dropped** from `/authorize` and `/token`. MCP clients send
  `resource=<this server>` (RFC 8707), allauth binds the token to it, and wger's
  API then refuses that token with 403 "Invalid target resource" — on every
  call. It has to go from `/token` too: allauth binds a resource named there
  even when the authorization request had none, including on refresh. Rewriting
  it to wger's API URL instead was rejected: allauth compares against the URL as
  Django rebuilds it, and behind a proxy a scheme mismatch alone would bring
  the 403 back. With an external IdP `resource` is passed through, since there
  the token is meant for this server.

### Identity comes with the check, keyed by a fingerprint

The same lookup names the caller, which serves logging and the optional
allowlist. It uses `/api/v2/userprofile/`, which needs only `api:read`, rather
than OIDC `userinfo`, which would need the `profile` scope this server does not
request. The cache key and the identity's subject are a SHA-256 prefix of the
token; the raw token is never logged and never used as a key.

### Rejections are classified by body, not status

wger never answers its own tokens with `401`: `SessionAuthentication` heads its
DRF authentication classes, so DRF turns every authentication failure into a
`403`. Measured against wger 2.7, an expired, revoked or unknown token and a
deactivated user all give `403 {"code": "token_not_valid"}` (simplejwt, last in
the chain), a missing scope gives `403` naming it (`The access token is missing
the "api:write" scope.`), and a token bound to another resource `403 Invalid
target resource.` The status code alone therefore says nothing; the body does.

A model that receives a bare rejection retries, and the retry fails
identically: the token is the caller's own, and only a new authorization can
produce a live one. So `api_err` attaches a hint saying the connection must be
authorized again, and for a missing scope names it — a user who granted only
`api:read` would otherwise watch every write tool fail opaquely. The token
check maps the same way: a named scope becomes `403 insufficient_scope`, any
other rejection `401 invalid_token`, which is what makes a client refresh.

### The HTTP transport is stateless

In a stateful MCP session every tool call runs in the context of the request
that opened the session, so the bound identity — and with it the forwarded
token — would be the first request's for the session's whole life. After the
hourly refresh the expired token kept going out, and since this mode did not
check the token locally then, any bearer plus a known session id acted as the
session's owner. The server uses nothing a session provides (no server-initiated
requests, no resumable streams; responses are plain JSON already), so it runs
stateless: each request carries, and is served with, its own token. This
applies to `oidc` too, which had the same flaw with the exchanged credential.

### `oidc` stays; the default does not change

`oidc` remains for wger < 2.7 and for deployments already fronted by
Keycloak/Authentik, and `static_token` for single-user self-hosting. The
built-in default stays `oidc`: an existing deployment has `OIDC_*` configured
and `MCP_AUTH` possibly unset, and flipping the default would silently turn it
into a pass-through server whose tokens wger rejects. `wger_oidc` is the
documented recommendation instead, set explicitly.

## Considered options

- **Introspection, or JWT-format access tokens.** The standard ways to check a
  token at the door. Introspection is off by default in allauth and needs client
  credentials this server does not have; JWTs need a wger-side format change
  plus key distribution. The profile lookup gets the same answer — and the
  username — with neither.
- **Leave the check to wger's API call.** The first version of this ADR. Fails
  the refresh, see above.
- **Replace the exchange rather than add a strategy.** Cleaner, one code path
  less. Refused: it would strand wger 2.6 deployments and everyone whose users
  authenticate through a corporate SSO.
- **Drop the AS facade and advertise wger directly.** Spec-correct, one hop
  shorter — and broken for claude.ai. Available as `MCP_AS_FACADE=false` for
  deployments whose clients all follow the pointer.
- **Ask wger for `profile` as well, and read the username from `userinfo`.**
  The textbook way to name the caller, but it widens the grant for an answer
  `userprofile` gives under `api:read`.

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
- Every token costs wger a lookup per minute, and every request a token lookup
  on the API call itself; the fan-out tools issue many requests in parallel. Watch wger's throttle counters; the semaphore caps in
  the tool modules are the lever on this side.
- Startup now depends on wger: the facade's endpoints come from its discovery
  document. The server retries for about half a minute (connection errors and
  5xx only), then exits with one line instead of a traceback.
- This server still stores nothing: no database, no per-user secrets. The only
  cache is username-by-fingerprint, in memory, per process, for a minute.
