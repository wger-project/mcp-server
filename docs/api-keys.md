# Credentials reference

This server deals with **two directions** of credential. They are not
interchangeable, and mixing them up is the most common cause of a `401` when
setting the server up for the first time.

| Direction | Variable | Who presents it | Used with |
|---|---|---|---|
| **Inbound** (client → MCP) | an IdP-issued OIDC token | the MCP client | `MCP_AUTH=oidc` |
| **Inbound** (client → MCP) | `MCP_STATIC_TOKEN` | the MCP client | `MCP_AUTH=static_token` |
| **Outbound** (MCP → wger) | derived per request via token exchange | this server | `MCP_AUTH=oidc` |
| **Outbound** (MCP → wger) | `WGER_API_KEY` | this server | `static_token`, `none` |

**Never hand an outbound credential to a client.** `WGER_API_KEY` grants full
access to a wger account; it should never leave the server.

See the [README](../README.md#inbound-auth-strategies) for how to choose a
strategy.

---

## Inbound credentials

### `oidc` — an IdP-issued token

The client obtains a token from the identity provider, either through
MCP-native OAuth (the client runs the flow itself) or out-of-band (see
`scripts/get_token.py` for a device-flow example). The server validates it
against the IdP's JWKS.

Relevant settings:

- The token must carry a `sub` claim; it identifies the caller and keys the
  per-user wger credential cache. Tokens without one are rejected.
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
| `401` under `static_token` with a token that looks right | Whitespace or quoting in `MCP_STATIC_TOKEN`, or the client is sending the wger API key by mistake. |
| Server starts, then every wger call fails | Outbound credential wrong: `WGER_API_KEY` invalid, or `WGER_BASE_URL` points at a different instance than the key belongs to. |
| `Requested audience not available` during exchange | The IdP client lacks an audience mapper for `WGER_OIDC_AUDIENCE`. |
| Exchange succeeds but wger rejects the token | `WGER_ALLAUTH_PROVIDER` does not match wger's `SocialApp.provider_id`. |
| Exchange fails only for some users | Those users have wger-side MFA enrolled — it must be delegated to the IdP instead. See the README. |
