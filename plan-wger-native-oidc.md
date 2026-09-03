# Plan: native wger OIDC (drop the token exchange on wger >= 2.7)

**Repos:** `wger/mcp-server` (this one), `wger/wger` (server), `wger/docs`
**Noted:** 2026-08-26
**Target:** the auth model mcp.wger.de launches with
**Origin:** wger 2.7 ships `allauth.idp.oidc`; the "make wger itself an OIDC
provider" item in `docs/HANDOFF.md` is done on the server side.

## Why

The whole token-exchange machinery in [ADR 0001](docs/adr/0001-multi-user-auth-via-oidc-token-exchange.md)
exists for one reason: wger's REST API only accepted wger-native credentials, so
an IdP token had to be traded for one. That is no longer true. wger 2.7 is an
OAuth2/OIDC provider *and* its API accepts the access tokens it issues.

For this server that means it stops being a broker and becomes a plain resource
server: take the caller's wger token, put it on the outbound `/api/v2/` call,
done. Three things fall away with it:

- the third-party IdP — the single biggest obstacle for a public deployment and
  for self-hosters,
- the RFC 8693 exchange plus the allauth headless `provider/token` login
  (`auth/exchange.py` is not used in this mode),
- **the wger-side MFA blocker** (HANDOFF #4). The authorization-code flow runs in
  the user's browser through wger's normal login, so TOTP/WebAuthn enrolled in
  wger simply works. This is what makes a public mcp.wger.de viable at all.

## What wger already provides (verified against the 2.7 branch)

| | |
|---|---|
| App | `allauth.idp.oidc` in `INSTALLED_APPS` — `settings/settings_global.py:144` |
| Adapter | `wger.utils.oidc_auth.WgerOIDCAdapter` — `settings_global.py:305` |
| API auth | `wger.utils.oidc_auth.OidcTokenAuthentication` in `DEFAULT_AUTHENTICATION_CLASSES` — `settings_global.py:507` |
| Scopes | `api:read` for `SAFE_METHODS`, `api:write` for everything else; enforced in the authentication class, not a permission class, so no viewset can skip it |
| On/off | `IDP_OIDC_PRIVATE_KEY` (env, `settings/main.py:126`); empty = provider off and previously issued tokens stop working. Key from `./manage.py generate-oidc-key` |
| Discovery | `/.well-known/openid-configuration`, keys at `/.well-known/jwks.json` (both CORS-open) |
| Endpoints | authorize `/identity/o/authorize` (wger's own guarded view, `wger/urls.py:310`), token `/identity/o/api/token`, userinfo `…/userinfo`, revoke `…/revoke`, device code `…/device/code` |
| Clients | `allauth.idp.oidc.models.Client` rows, created from the shell — see `docs/administration/oauth2_provider.rst`; `PUBLIC` (PKCE, no secret) or `CONFIDENTIAL`, per-client `redirect_uris`, `scopes`, `skip_consent`, `cors_origins`, `allow_uri_wildcards` |
| Token format | **opaque** — allauth default `ACCESS_TOKEN_FORMAT="opaque"`, wger does not override |
| Lifetimes | access 3600 s (allauth default), refresh 120 d (`settings_global.py:314`), rotation on |
| PKCE | `S256` advertised |
| Resource indicators | RFC 8707 parsed and accepted; `validate_resource_uris()` is a no-op by default. `aud` is only written into JWT-format tokens, so it is invisible with opaque ones |
| Introspection | off by default (`INTROSPECTION_ENABLED=False`) |
| DCR | off by default, and `DCR_REQUIRES_INITIAL_ACCESS_TOKEN=True` by default; rate-limited `3/m/ip` when on |
| DCR scopes | **whatever the registering client asks for**, stored verbatim (`forms.py:239 clean_scope`), defaulting to `["openid"]` alone. No allowlist, no server-side default. At `/authorize` the requested scopes must be a subset of the client's (`internal/oauthlib/request_validator.py:59`), so a client registered without `api:read` fails with `invalid_scope` |
| Housekeeping | `./manage.py oidc_cleartokens`, daily under Celery |

## Design decisions

**D1 — Pass the token through, do not validate it locally.**
Access tokens are opaque, so there is nothing to verify against JWKS; the
existing `oidc` validation path does not apply. wger is the only authority on
whether a token is live and which scopes it carries, and it checks on every
call anyway. So: no introspection round-trip, no second source of truth. The
price is that a bad token is only noticed at the first API call — which is why
error mapping (phase 3) is part of this plan and not an afterthought.

**D2 — Keep the AS facade, point it at wger.**
Its original justification (a private IdP) is gone, but the client limitation
that drove [ADR 0003](docs/adr/0003-oauth-authorization-server-facade.md) is
not: claude.ai treats the MCP origin as the authorization server and ignores
the `authorization_servers` pointer. The facade code is already generic — it
takes an authorization and a token endpoint — so it only needs different
endpoints, discovered from `WGER_BASE_URL`.

**D3 — New strategy `MCP_AUTH=wger_oidc`, next to the existing three.**
`oidc` stays for setups fronted by Keycloak/Authentik (and for wger < 2.7),
`static_token` stays for single-user self-hosting without a provider key.
`wger_oidc` becomes the documented default for wger >= 2.7 and the mode
mcp.wger.de runs.

**D4 — Request `openid api:read api:write`.**
`openid` for the identity, both API scopes because roughly half the tool surface
writes. A read-only deployment (`api:read` only, paired with a read-only
`MCP_TOOLS` selection) is a later refinement, not part of this plan.

**D5 — Identity is resolved lazily from `userinfo`, keyed by a token
fingerprint.** With opaque tokens the middleware cannot know who is calling.
Nothing in the request path needs the username except logging and the optional
allowlist, so fetch it on first use per token and cache it. Never log or cache
the raw token — SHA-256 prefix only.

**D6 — Enable DCR on wger.de.** Without it every client type needs a `Client`
row and users have to paste a client id (claude.ai can, Claude Code and
`mcp-remote` normally cannot). With it the connector flow is one URL. allauth's
`3/m/ip` limit covers the obvious abuse; verify what scopes and grant types a
self-registered client gets before switching it on.

**D7 — No new state.** Still no database, still no per-user secrets on this
side. The only cache is username-by-fingerprint, in memory, per process.

## Work items

### Phase 0 — wger side (server repo), prerequisites

- [ ] Scope policy for DCR clients in `WgerOIDCAdapter.validate_client_registration()`
      (`wger/utils/oidc_auth.py`). The hook runs on the *unsaved* `Client` right
      before `save()` (`views.py:684-699`), so it can both reject and shape:
      reject scopes outside `{openid, profile, email, api:read, api:write}`,
      force `skip_consent = False`, optionally cap grant types. The DCR response
      serialises `client.get_scopes()`, so a clamp is reported back honestly.
      Note there is otherwise **no ceiling**: anyone can self-register a client
      asking for `api:write` with any redirect URI — the consent screen and the
      login are the only gate, which is exactly why `skip_consent` must stay off.
- [ ] Settings for DCR once decided: `IDP_OIDC_DCR_ENABLED`,
      `IDP_OIDC_DCR_REQUIRES_INITIAL_ACCESS_TOKEN`, and possibly a tighter
      `IDP_OIDC_RATE_LIMITS['client_registration']` (default `3/m/ip`).
- [ ] Generate and set `IDP_OIDC_PRIVATE_KEY` on wger.de (`generate-oidc-key`),
      store it like any other secret; rotating it logs every connected app out.
- [ ] If DCR stays off: create the `Client` rows — claude.ai (`CONFIDENTIAL`,
      redirect `https://claude.ai/api/mcp/auth_callback`), a `PUBLIC` one for
      local clients (loopback redirects, needs `allow_uri_wildcards`), one per
      further client. Decide `skip_consent` per client (off for third-party).
- [x] Confirm `oidc_cleartokens` runs — it does: `flush_expired_oidc_tokens_task`
      is a daily Celery-beat task (`wger/core/tasks.py:119`) and the docs say so.
      Only the ops question is left: is Celery actually running on wger.de.
- [x] **A "connected applications" page in the user profile.** Built
      2026-08-28 in the server repo (uncommitted): `wger/core/views/oidc.py` +
      `templates/user/connected_applications.html`, linked from *Settings* when
      the provider is configured, 17 tests. Nothing of allauth's had to be
      overridden - it ships no templates and no user-facing view at all. Two
      findings worth keeping: rotation *deletes* the old refresh-token row, so
      there is no durable "connected since" to show; and disconnecting has to
      delete every token type, or a pending authorization code stays
      redeemable. Documented in `manual/connected_applications.rst` (new) with
      a pointer from `administration/oauth2_provider.rst`.
- [x] Check the DRF throttle scope that applies to token-authenticated traffic.
      **The worry was the wrong way round.** wger uses `ScopedRateThrottle`,
      which only bites on views that declare a `throttle_scope`: `ingredient_*`,
      `exercise_create`, `login`. Routines, logs, sessions and measurements have
      *no* ceiling at all. The only tool that can touch one is
      `nutrition_summary` via `ingredient_detail` (300/min per user, 8 parallel)
      — not a problem. So nothing to raise; the open question is instead whether
      a public launch wants *some* ceiling on write traffic, given each opaque
      token also costs a DB lookup. Phase 6 material, not a blocker.

### Phase 1 — the strategy

- [x] `config.py`: add `wger_oidc` to `AuthStrategy` (:55). Validator branch in
      `_check_strategy_requirements` (:309) requiring **only** `WGER_BASE_URL` —
      explicitly *not* `OIDC_CLIENT_ID`/`SECRET`/`WGER_OIDC_AUDIENCE`.
- [x] `config.py`: new `mcp_wger_scopes: list[str] = ["openid", "api:read", "api:write"]`
      (reuse the comma-splitting validator); reuse `mcp_oidc_allowed_users`.
- [x] `auth/wger_oidc.py`: `WgerBearerMiddleware` modelled on `StaticTokenMiddleware`
      (`auth/base.py`) — extract the bearer, 401 with the `WWW-Authenticate` +
      `resource_metadata=` header built the way `auth/oidc.py:89` does it, bind
      `Identity(subject=<fingerprint>, inbound_token=token, strategy="wger_oidc")`,
      reuse `is_bypass_path` incl. the facade paths.
- [x] `auth/exchange.py`: give `WgerTokenProvider` a pass-through mode returning
      `Bearer <identity.inbound_token>` (~10 lines around :220). Keeping it in the
      same class means `api_client.py:40` and the lifespan teardown stay untouched.
- [x] `auth/__init__.py`: cases for the new strategy in `build_auth_middleware`,
      `build_token_provider`, `build_authorization_server_facade`; update the
      module docstring.
- [x] Username resolution: fetch `/identity/o/api/userinfo` once per token
      fingerprint (or `/api/v2/userprofile/`), cache in memory, apply
      `mcp_oidc_allowed_users` when set.

### Phase 2 — discovery, metadata, facade

- [x] Reuse `oidc_discovery.discover_endpoints()` against `WGER_BASE_URL` — wger's
      discovery document already yields authorize/token/jwks, so no new code.
- [x] `asfacade.metadata()`: add `api:read`/`api:write` to `scopes_supported`, keep
      `S256`, and advertise `registration_endpoint` when DCR is on.
- [x] If DCR is on: proxy `/register` through the facade the same way `/token` is
      proxied, so clients only ever talk to one origin — **and inject the API
      scopes into the proxied registration**. A generic MCP client has no idea
      wger uses `api:read`/`api:write`; if it registers with `openid` alone, its
      later `/authorize` is rejected with `invalid_scope` and the connector is
      dead. This server does know which scopes it needs, and it is the party the
      client thinks it is registering with, so adding them to the proxied
      `scope` (and to the `/authorize` query when absent) is the fix that does
      not depend on every client reading `scopes_supported`.
- [x] `auth/oauth.py`: `protected_resource_metadata()` keeps advertising this
      origin (facade); add `scopes_supported`. Consider a switch to advertise
      wger directly for clients that *do* follow the pointer — one setting,
      `MCP_AS_FACADE=on|off`, default on.
- [x] Pass `resource=<MCP_PUBLIC_URL>/mcp` through untouched (it already is —
      `authorize()` forwards the query verbatim); note that wger accepts it.

### Phase 3 — error mapping

- [x] 401 from wger must reach the client as "re-authenticate", not as a generic
      tool error. **Done as the stated minimum only**: `api_err` attaches a hint
      naming expiry and saying a retry will not help. The louder option — some
      FastMCP-level signal that turns a mid-session upstream 401 into a
      protocol-level auth failure over streamable HTTP — was *not* investigated;
      there is no obvious hook for it, and a tool returning an error dict is the
      only channel a tool has. Worth another look if clients keep retrying.
- [x] 403 with `insufficient_scope`: say which scope is missing and that the
      connection has to be re-authorized — a user who granted only `api:read`
      will otherwise see every write tool fail opaquely.
- [x] Audit that no code path logs the bearer or puts it in an error payload.

### Phase 4 — tests

Mirror the existing suites (`respx`, no network):

- [x] `tests/test_auth_wger_oidc.py` — missing/malformed bearer → 401 incl. the
      `WWW-Authenticate` header; valid bearer → identity bound; allowlist reject;
      bypass paths stay public.
- [x] Pass-through: outbound `/api/v2/` call carries exactly the inbound token.
- [x] 401/403 from wger → the mapped tool error.
- [x] Facade against wger endpoints (extend `tests/test_as_facade.py`).
- [x] Config: `wger_oidc` needs no client credentials; stdio still refuses it.
- [x] Username cache: one lookup for N requests with the same token — sequential
      and concurrent. (Against `/api/v2/userprofile/`, not `userinfo`: the latter
      only names the user when the grant carries `profile`, which D4 does not
      request.)

### Phase 5 — docs

- [x] README: rewrite *How auth works* — the sequence diagram loses two hops.
      New strategy table row, wger >= 2.7 as the recommended path, the old `oidc`
      row marked as "external IdP / wger < 2.7".
- [x] `.env.example`: a `wger_oidc` block; make clear which OIDC_* vars are only
      for the external-IdP mode.
- [x] ADR 0005 "native wger OIDC", superseding the *transport* half of ADR 0001
      (0001 stays as the record of why the exchange existed).
- [x] `docs/HANDOFF.md`: tick off "make wger itself an OIDC provider", drop the
      MFA constraint for this mode, note the opaque-token consequence.
- [x] `docs/api-keys.md`: which credential goes where, now with four modes.
- [~] wger docs (`docs/administration/oauth2_provider.rst`): *Revoking access*
      added, pointing at the new manual chapter. Still missing: the "connecting
      the MCP server" section, and DCR once it is decided.
- [~] A user-facing "connect your assistant" page. `manual/connected_applications.rst`
      (new) covers the concept: what the consent screen means, that `api:write`
      is write access to *everything*, how to disconnect, and a note that an AI
      assistant carries the data to whichever service runs the model. Still
      missing: the per-client recipes (claude.ai connector, `claude mcp add`,
      editors with an MCP config file, `mcp-remote` for stdio-only clients).

### Phase 6 — deploying mcp.wger.de

- [ ] DNS A/AAAA for `mcp.wger.de`, certificate.
- [ ] nginx: `proxy_buffering off`, `proxy_request_buffering off`,
      `proxy_read_timeout 3600s` (streamable HTTP/SSE); a rate limit in front.
- [ ] compose service from `compose.example.yml` into the wger.de stack, on the
      internal network, port bound to loopback only.
- [ ] Env: `MCP_AUTH=wger_oidc`, `WGER_BASE_URL=<internal wger URL>`,
      `MCP_PUBLIC_URL=https://mcp.wger.de`, `ALLOWED_HOSTS=mcp.wger.de`.
- [ ] Tag a release first — `pyproject.toml` is at 0.1.0 and nothing is tagged;
      deploy a pinned tag, never `:latest`. PyPI publishes on a GitHub *release*,
      the ghcr image on the `v*.*.*` tag.
- [ ] Smoke test before announcing:
      `curl https://mcp.wger.de/health`,
      `curl https://mcp.wger.de/.well-known/oauth-protected-resource`,
      `curl https://mcp.wger.de/.well-known/oauth-authorization-server`,
      then a real claude.ai connector round-trip incl. consent screen, a read
      tool, a write tool, and behaviour after the access token expires.
- [ ] Monitoring on `/health`; watch wger's throttle counters after launch.
- [~] Say plainly, on the connect page and near the consent screen, that the
      data travels through this server to whichever model provider the user
      connected. The manual chapter says it; the connect page does not exist yet,
      and the consent screen itself still says nothing.

### Phase 7 — follow-ups, not in this pass

- Read-only deployments: `api:read` only, coupled to `MCP_TOOLS`.
- Introspection or JWT-format access tokens, if early rejection ever matters
  more than the extra round-trip.
- Revocation: on disconnect the client should hit wger's `revoke` endpoint.
- Per-tool scope declarations, once MCP has a convention for it.

## Status (2026-08-28)

Phases 1-4 are implemented and tested in `mcp-server` (275 tests pass; the 6
failures in `test_workout_sessions.py` and the collection error in
`test_measurement_fields.py` predate this work and come from drift in the
locally-editable `wger_api_client`). Phase 5 is done for this repo's docs; the
two wger-repo docs items are not. Phases 0, 6 and 7 are untouched.

Decisions taken while implementing, beyond what the plan specified:

- **DCR needs no setting here.** allauth publishes `registration_endpoint` in the
  discovery document exactly when DCR is enabled, so the facade advertises and
  proxies `/register` iff wger does. Open question 1 is therefore only a wger-side
  decision now.
- **`MCP_AUTH` keeps defaulting to `oidc`.** Flipping it would silently turn an
  existing IdP deployment (which has `OIDC_*` set but may leave `MCP_AUTH` unset)
  into a pass-through server whose tokens wger rejects. `wger_oidc` is documented
  as the recommended value instead. Worth revisiting for a 1.0.
- **Scopes are added to `/authorize`, not only when absent.** The plan's wording
  allowed the narrower reading; the union is what actually works, since a client
  asking for `openid` alone would otherwise get a token that fails every call.
- **Endpoint discovery is memoised per process** (`auth.reset_endpoint_cache()`
  for tests) — three call sites now want the answer that one startup fetch gives.
- **`OIDC_AUTHORIZATION_ENDPOINT` matters under `wger_oidc`** and is documented:
  phase 6 puts `WGER_BASE_URL` on an internal URL, and discovery against an
  internal host returns internal URLs — but `/authorize` is followed by the
  user's *browser*.

## Open questions

1. **DCR on or off** — blocks D6 and the phase-0 client setup. The scope
   question is answered (see the table): it is the *client* that names its
   scopes, so DCR only works if the facade injects the API scopes or the client
   reads `scopes_supported`. Remaining risk is behavioural: which MCP clients
   actually attempt DCR, and whether they reuse the scope string the
   registration returned. Test against claude.ai, Claude Code and `mcp-remote`
   before deciding.
2. **Consent screen for a first-party MCP client** — `skip_consent` is
   defensible for an official wger client, but the consent page is also the only
   moment a user sees that they are handing over write access. Recommendation:
   leave it on.
3. **Does anything still need the old `oidc` mode?** If wger.de is the only
   deployment that matters, the exchange path could eventually be dropped
   instead of maintained; keep it for now — self-hosters on 2.6 and
   Keycloak-fronted setups exist.
4. **Token lifetime** — 1 h access / 120 d refresh is generous for a credential
   that a third-party assistant holds. Consider a shorter access-token lifetime
   for MCP clients specifically, if allauth allows it per client.
