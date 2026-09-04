# Handoff notes

Context for the wger maintainers taking this over. Everything here is either
non-obvious from the code or a decision worth revisiting with more context than
the original author had.

## What this is

A standalone MCP server that talks to wger over its **public REST API**. It
requires no changes to wger and runs as a separate process. wger >= 2.6 is
required because the auth model depends on allauth's headless
`provider/token` endpoint.

## Things that cost time to discover

These are written down because each one was found the hard way.

1. **claude.ai ignores the advertised `authorization_endpoint`.** It assumes the
   conventional root paths `/authorize` and `/token` on the MCP origin,
   regardless of what the authorization-server metadata says. That is why the
   AS facade exists ([ADR 0003](adr/0003-oauth-authorization-server-facade.md))
   and why its paths are environment-configurable rather than fixed.

2. **wger's REST `/ingredient/` is read-only.** The Open Food Facts tools can
   look up a barcode, but nothing can write it back: the generated client has
   no `ingredient_create` and no `IngredientRequest` model, because the schema
   exposes none. The old web-form path was dropped with the move to multi-user
   auth, so `create_ingredient` was removed. **If wger ever gains a writable
   ingredient endpoint, that tool should come back.** The `wger_ingredient_payload`
   that used to be kept in `tools/off.py` for exactly that day is gone — it
   repeated `macros_per_100g` under different keys in every response, and
   renaming those keys is the small half of writing that tool.

3. **`SocialApp.provider_id` is not reliably `openid_connect`.** It is whatever
   slug the admin configured. Hard-coding it produces a confusing failure deep
   in the exchange, hence the `WGER_ALLAUTH_PROVIDER` setting.

4. **wger-side MFA blocks the headless exchange entirely.** If a user has a
   TOTP/WebAuthn authenticator enrolled *in wger*, `provider/token` returns a
   pending MFA challenge and no JWT — a server-side exchange cannot complete it,
   and no setting skips it. MFA has to be delegated to the IdP. This is the
   single biggest constraint on the multi-user model and is worth fixing
   upstream if the MCP is to be broadly usable.

5. **OFF returns `""` rather than omitting per-language fields.** Verified
   against live data. Any code reading `product_name_<lang>` must treat empty
   string and missing key alike.

## Open items / suggested next steps

- **Make wger itself an OIDC provider** (via django-allauth). This is the
  highest-value follow-up. Today, multi-user requires a third-party IdP, which
  most self-hosters will not run — so in practice they fall back to the
  single-user `static_token` strategy. If wger could issue OIDC tokens itself,
  multi-user would work with no external dependency. That is a wger-side change,
  which is why it was not attempted here.

- **Per-user API keys as an MFA-compatible fallback.** For deployments that
  require wger-enforced MFA, the token-exchange model is unusable (see above).
  Per-user wger API keys would work, but there is no API to provision them and
  it would mean storing a secret per user server-side.

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

- 59 tests, no network access required — `respx` mocks all outbound HTTP.
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

- **No database, no persistent state.** The wger JWT cache is in-memory and
  per-process. Running multiple replicas just means each derives its own tokens.
