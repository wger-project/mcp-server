# Handoff notes

Context for the wger maintainers taking this over. Everything here is either
non-obvious from the code or a decision worth revisiting with more context than
the original author had.

## What this is

A standalone MCP server that talks to wger over its **public REST API**. It
requires no changes to wger and runs as a separate process. wger >= 2.6 is
required; on 2.6 the auth model depends on allauth's headless `provider/token`
endpoint, on >= 2.7 it uses wger's own OIDC provider instead
([ADR 0005](adr/0005-native-wger-oidc.md)).

## Things that cost time to discover

These are written down because each one was found the hard way.

1. **claude.ai ignores the advertised `authorization_endpoint`.** It assumes the
   conventional root paths `/authorize` and `/token` on the MCP origin,
   regardless of what the authorization-server metadata says. That is why the
   AS facade exists ([ADR 0003](adr/0003-oauth-authorization-server-facade.md))
   and why its paths are environment-configurable rather than fixed.

2. **wger's REST `/ingredient/` is read-only.** The Open Food Facts tools can
   look up a barcode and shape the macros into a wger-compatible payload, but
   they cannot write it back. The old web-form path was dropped with the move to
   multi-user auth, so `create_ingredient` was removed. **If wger ever gains a
   writable ingredient endpoint, that tool should come back** — the payload
   shaping is already there (`wger_ingredient_payload` in `tools/off.py`).

3. **`SocialApp.provider_id` is not reliably `openid_connect`.** It is whatever
   slug the admin configured. Hard-coding it produces a confusing failure deep
   in the exchange, hence the `WGER_ALLAUTH_PROVIDER` setting.

4. **wger-side MFA blocks the headless exchange entirely** — in `MCP_AUTH=oidc`
   only. If a user has a TOTP/WebAuthn authenticator enrolled *in wger*,
   `provider/token` returns a pending MFA challenge and no JWT: a server-side
   exchange cannot complete it, and no setting skips it, so MFA has to be
   delegated to the IdP. **`MCP_AUTH=wger_oidc` does not have this problem** —
   the authorization-code flow runs in the user's browser through wger's own
   login page, so whatever MFA they enrolled applies normally. This constraint
   is now a reason to prefer `wger_oidc`, not a blocker on the model as a whole.

5. **OFF returns `""` rather than omitting per-language fields.** Verified
   against live data. Any code reading `product_name_<lang>` must treat empty
   string and missing key alike.

6. **wger's access tokens are opaque**, and `ACCESS_TOKEN_FORMAT` is left at
   allauth's default. So under `wger_oidc` there is nothing to validate locally:
   no JWKS check, no claims, no username in the token. Two things follow —
   a bad token surfaces at the first API call rather than at the door (hence the
   401/403 hint in `tools/common.api_err`), and naming the caller costs a
   request to `/api/v2/userprofile/`, which is why it only happens when
   `MCP_OIDC_ALLOWED_USERS` is set.

7. **A DCR client gets whatever scopes it asks for.** allauth's
   `clean_scope` stores the requested list verbatim and defaults to `["openid"]`
   alone; at `/authorize` the requested scopes must then be a subset of the
   client's. So a client that self-registers without `api:read` is refused later
   with `invalid_scope` and nothing says why. The AS facade adds the API scopes
   to both the proxied registration and the authorize query for exactly this
   reason. On the wger side, `validate_client_registration()` is the hook that
   can clamp what a self-registered client may ask for — and `skip_consent` must
   stay off, since the consent screen and the login are the only gate.

## Open items / suggested next steps

- ~~**Make wger itself an OIDC provider**~~ — **done in wger 2.7**
  (`allauth.idp.oidc`). This server supports it as `MCP_AUTH=wger_oidc`
  ([ADR 0005](adr/0005-native-wger-oidc.md)): multi-user with no external
  dependency and no client credentials. What remains is on the wger side:

  - **A "connected applications" page in the user profile.** allauth ships none —
    there is `TokenAdmin` in the Django admin and a `revoke` endpoint for
    clients, but nothing a user can reach. Granting an assistant write access to
    every training, nutrition and body record without being able to take it back
    is the most visible gap for a public launch. A small view over
    `Token.objects.filter(user=...)` grouped by client, with a delete button.
  - **A scope policy for DCR clients**, if dynamic client registration is
    switched on: `WgerOIDCAdapter.validate_client_registration()` runs on the
    unsaved `Client` and can both reject and shape — refuse scopes outside
    `{openid, profile, email, api:read, api:write}`, force `skip_consent=False`.
    Without it, anyone can self-register a client asking for `api:write` with any
    redirect URI; the consent screen and the login are then the only gate.
  - **Throttle headroom.** Each opaque token costs a DB lookup, and the fan-out
    tools (`nutrition_summary`, `volume_trend`, `compare_periods`,
    `list_slot_entry_configs`) issue many requests in parallel. Check which DRF
    throttle scope applies to token-authenticated traffic before a public
    launch.
  - **Token lifetimes.** 1 h access / 120 d refresh is generous for a credential
    a third-party assistant holds; consider shortening it for MCP clients.
  - **`oidc_cleartokens`** has to run (Celery beat or cron), or expired tokens
    accumulate forever.

- **Per-user API keys as an MFA-compatible fallback.** Only still relevant for
  wger 2.6 deployments, where the token-exchange model cannot complete a
  wger-side MFA challenge (see above); `wger_oidc` solves it on 2.7. There is no
  API to provision such keys and it would mean storing a secret per user
  server-side.

- **Revocation on disconnect.** When a client disconnects, this server could
  call wger's `revoke` endpoint rather than leaving the grant live until it
  expires. Needs a disconnect signal from MCP, which there currently is not.

- **Ingredient creation**, if the REST API gains write support (see #2).

- **Release/versioning.** `pyproject.toml` is at `0.1.0` and nothing has been
  tagged. CI publishes `ghcr.io/<repo>:latest` on the default branch and
  semver tags on `v*.*.*`. Pick a versioning policy before the first release;
  note the OFF response keys changed once already (see the README's *Upgrading*
  section), and there is no compatibility shim.

- **CI triggers on `master`**, matching this repository's default branch. It was
  developed on a fork whose default branch was `main`, so if you see a stale
  reference to `main` anywhere, that is why.

## Testing notes

- No network access required — `respx` mocks all outbound HTTP.
- The MCP `initialize` request is the cheapest way to exercise the whole
  middleware chain end-to-end.
- Fixtures use `example.com` hosts and obviously-fake credentials throughout.
- The suite runs on Python 3.11, 3.12 and 3.13 in CI.

## Things deliberately not done

- **No aliases for the renamed Open Food Facts response keys.** `name_pl` became
  `name_localized` and `ingredients_text_pl` became `ingredients_text` when the
  hard-coded Polish handling was made configurable. Since nothing had been
  released, this was a clean break rather than a deprecation. If you would
  rather ship aliases for a transition period, that is a small change in
  `tools/off.py::_shape`.

- **No database, no persistent state.** Both caches — the wger JWT under `oidc`,
  the username-by-token-fingerprint under `wger_oidc` — are in-memory and
  per-process. Running multiple replicas just means each fills its own.
