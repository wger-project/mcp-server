# Credentials reference

This server deals with **two directions** of credential. They are not
interchangeable, and mixing them up is the most common cause of a `401` when
setting the server up for the first time.

| Direction | Credential | Who presents it | Used with |
|---|---|---|---|
| **Inbound** (client → MCP) | a wger-issued OAuth access token | the MCP client | `MCP_AUTH=wger_oidc` |
| **Inbound** (client → MCP) | an IdP-issued OIDC token | the MCP client | `MCP_AUTH=oidc` |
| **Inbound** (client → MCP) | `MCP_STATIC_TOKEN` | the MCP client | `MCP_AUTH=static_token` |
| **Outbound** (MCP → wger) | the inbound token, unchanged | this server | `MCP_AUTH=wger_oidc` |
| **Outbound** (MCP → wger) | derived per request via token exchange | this server | `MCP_AUTH=oidc` |
| **Outbound** (MCP → wger) | `WGER_API_KEY` | this server | `static_token`, `none` |

Under `wger_oidc` the two directions carry the *same* token, which is the point
of the mode: wger issued it and wger accepts it, so there is nothing to
translate.

**Never hand an outbound credential to a client.** `WGER_API_KEY` grants full
access to a wger account; it should never leave the server.

See the [README](../README.md#inbound-auth-strategies) for how to choose a
strategy.

---

## Inbound credentials

### `wger_oidc` — a wger-issued access token

The client obtains it from wger itself, through MCP-native OAuth against this
server's [AS facade](../README.md#authorization-server-facade), which proxies to
wger. The user logs in on wger's own login page — MFA included — and approves a
consent screen naming the scopes.

The token is **opaque**: this server does not and cannot validate it locally.
wger checks it, and its scopes, on every API call.

Relevant settings:

- `MCP_WGER_SCOPES` — what to ask wger for; default `openid api:read api:write`.
  `api:read` gates every read and `api:write` every write, so dropping the
  latter makes the deployment read-only.
- `MCP_OIDC_ALLOWED_USERS` — optional allowlist. Costs one
  `/api/v2/userprofile/` request per token, cached in memory, because an opaque
  token carries no username; without it no such lookup happens.

Nothing else is configured: no client id, no secret, no audience, no issuer.
See [ADR 0005](adr/0005-native-wger-oidc.md).

### `oidc` — an IdP-issued token

The client obtains a token from the identity provider, either through
MCP-native OAuth (the client runs the flow itself) or out-of-band (see
`scripts/get_token.py` for a device-flow example). The server validates it
against the IdP's JWKS.

Relevant settings:

- `MCP_OIDC_AUDIENCE` — if set, the token's `aud` (or `azp`) must match.
- `MCP_OIDC_USERNAME_CLAIM` — which claim names the user, default
  `preferred_username`.
- `MCP_OIDC_ALLOWED_USERS` — optional allowlist; empty means any authenticated
  user of that IdP.

Because the identity travels with each request, every user acts as their **own**
wger account.

### `static_token` — a shared secret

```bash
openssl rand -hex 32
```

Put the result in `MCP_STATIC_TOKEN`; the client sends it as
`Authorization: Bearer <token>`. Minimum 32 characters, enforced at startup.

This is a single-user setup: everyone presenting the secret acts as the one
wger account behind `WGER_API_KEY`. Rotate by changing the variable and
restarting.

---

## Outbound credentials

### Pass-through (`wger_oidc` only)

There is no outbound credential at all. The caller's token is copied onto the
`Authorization` header of the `/api/v2/*` call and nothing is stored or cached.

### `WGER_API_KEY` — a wger API key

Used by the `static_token` and `none` strategies. Get it from your wger
instance under **Settings → API → "API key"**. It is a DRF token, sent upstream
as `Authorization: Token <key>`.

One key belongs to one wger user, so the whole server acts as that user.

This variable used to be called `WGER_DEV_TOKEN`, a name that undersold what it
is. The old spelling is still accepted, so existing deployments need no change;
`WGER_API_KEY` wins if both are set.

### Token exchange (`oidc` only)

No long-lived outbound secret is stored per user. For each request the server:

1. exchanges the inbound token (RFC 8693) for one whose audience is wger's
   OIDC client, using its own confidential-client credentials
   (`OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET`);
2. posts that to wger's allauth headless `provider/token` endpoint;
3. uses the returned wger JWT as `Authorization: Bearer` on `/api/v2/*`.

The wger JWT is cached in memory (~5 min) per user and re-derived on expiry.
See [ADR 0001](adr/0001-multi-user-auth-via-oidc-token-exchange.md).

---

## Troubleshooting 401s

| Symptom | Likely cause |
|---|---|
| `401` from the MCP server, `www-authenticate: Bearer` | Inbound credential missing or wrong — check what the client is sending. |
| Tools return `401` with a *"connection has to be authorized again"* hint | `wger_oidc`: the access token expired (1 h) or was revoked. The client has to re-run the OAuth flow; a retry sends the same dead token. |
| Tools return `403` naming a scope | `wger_oidc`: the grant is missing `api:read` or `api:write`. Re-authorize the connection; check `MCP_WGER_SCOPES` if it keeps happening. |
| `invalid_scope` at wger's `/authorize` | The OAuth client row in wger does not carry the scopes being requested. Add `api:read`/`api:write` to it. |
| `404` from wger's `/.well-known/openid-configuration` at startup | The provider is not switched on in wger: `IDP_OIDC_PRIVATE_KEY` is unset (`./manage.py generate-oidc-key`), or wger is older than 2.7. |
| The browser cannot reach the URL `/authorize` redirects to | `WGER_BASE_URL` is an internal address, so discovery returned internal URLs. Set `OIDC_AUTHORIZATION_ENDPOINT` to wger's public authorize endpoint. |
| `401` under `static_token` with a token that looks right | Whitespace or quoting in `MCP_STATIC_TOKEN`, or the client is sending the wger API key by mistake. |
| Server starts, then every wger call fails | Outbound credential wrong: `WGER_API_KEY` invalid, or `WGER_BASE_URL` points at a different instance than the key belongs to. |
| `Requested audience not available` during exchange | The IdP client lacks an audience mapper for `WGER_OIDC_AUDIENCE`. |
| Exchange succeeds but wger rejects the token | `WGER_ALLAUTH_PROVIDER` does not match wger's `SocialApp.provider_id`. |
| Exchange fails only for some users | Those users have wger-side MFA enrolled — it must be delegated to the IdP instead. See the README. |
