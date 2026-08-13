# wger MCP server

An [MCP](https://modelcontextprotocol.io) server that exposes the [wger](https://wger.de) (>= 2.6) fitness/nutrition REST API as tools (routines, workout logging, exercise & ingredient catalog, nutrition plans + meals + recipes, diary, body-weight tracking, gym equipment, body measurements, volume/PR analytics, daily calorie calculator, …) so that AI assistants can read and write your wger data.

It talks to a wger instance over its public REST API — it is a separate service and requires no changes to wger itself.

- **Transport:** MCP **Streamable HTTP** (FastMCP).
- **Auth:** **multi-user via OIDC SSO** — any OIDC IdP (Keycloak, Authentik, Auth0, Okta, …). Every request acts as the calling user's own wger account. For single-user self-hosting without an IdP, [`MCP_AUTH=static_token`](#static_token--single-user-no-idp-required) takes a shared secret plus your wger API key instead.
- **Requires:** wger >= 2.6, Python >= 3.11.

## How auth works

wger 2.6 added OIDC SSO (allauth) and issues its own JWTs; its REST API only accepts wger-native credentials. So this server is **multi-user** and uses a shared OIDC identity provider (the same one wger logs in with). Per request:

```text
client → MCP    Authorization: Bearer <OIDC token>   (via MCP-native OAuth, or sent directly)
MCP             validates the token against the IdP's JWKS
MCP → IdP       RFC 8693 token-exchange → access_token aud'd at wger's OIDC client
MCP → wger      POST /allauth/app/v1/auth/provider/token  → a wger JWT
MCP → wger      Authorization: Bearer <wger JWT>  on /api/v2/*   (cached ~5 min per user)
```

Provider-agnostic: JWKS/token endpoints come from the IdP's discovery document (`{issuer}/.well-known/openid-configuration`). No per-user secrets are stored — the wger access token is cached in memory and re-derived on expiry. See [docs/adr/0001-multi-user-auth-via-oidc-token-exchange.md](docs/adr/0001-multi-user-auth-via-oidc-token-exchange.md).

## Quick start

```bash
git clone https://github.com/wger-project/mcp-server.git
cd mcp-server
uv sync
cp .env.example .env
# Edit .env: set WGER_BASE_URL, OIDC_ISSUER, OIDC_CLIENT_ID/SECRET, WGER_OIDC_AUDIENCE.
uv run wger-mcp
```

Server listens on `http://0.0.0.0:8765`, MCP endpoint at `/mcp`.

Just trying it against your own account? The [`static_token`](#static_token--single-user-no-idp-required) strategy needs only a wger API key and no IdP:

```bash
cp .env.example .env
# In .env: MCP_AUTH=static_token, MCP_STATIC_TOKEN=$(openssl rand -hex 32),
#          WGER_DEV_TOKEN=<your wger API key>, WGER_BASE_URL=<your wger>
uv run wger-mcp
```

## Prerequisites at the IdP & wger

- **wger** is configured with your IdP as an OIDC social-login provider (`WGER_SOCIAL_PROVIDERS`), so `provider/token` accepts its tokens. `WGER_ALLAUTH_PROVIDER` must match wger's provider id — the slug in wger's `SocialApp` (e.g. `keycloak` or `openid_connect`); it's the `<id>` in the OAuth callback path `/account/oidc/<id>/login/callback/`.
- **IdP** has a *confidential* client for this server (`OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET`) with **token-exchange (RFC 8693)** enabled and permitted to exchange to wger's client audience (`WGER_OIDC_AUDIENCE`). On Keycloak that means enabling *Standard Token Exchange* and adding an *Audience* mapper that includes the wger client (otherwise the exchange fails with `Requested audience not available`).
- **MFA is delegated to the IdP.** wger 2.6's headless `provider/token` enforces *wger-side* MFA: a user with a TOTP/WebAuthn authenticator enrolled **in wger** cannot complete the server-side login (no setting skips it). Users must rely on the IdP for MFA and **not** enroll wger-side 2FA. If wger-enforced MFA is a hard requirement, use per-user wger API keys instead of this exchange model.

## Inbound auth strategies

Pick one with `MCP_AUTH=`. The server gates **every** request to `/mcp/*`. `/health`, `/.well-known/*` and the AS-facade endpoints (`/authorize`, `/token` by default) are always public.

| Strategy | Users | Needs an IdP? | Safe to expose? |
|---|---|---|---|
| [`oidc`](#oidc-default) (default) | multi-user, each acts as themselves | yes | yes |
| [`static_token`](#static_token--single-user-no-idp-required) | single-user (shared wger account) | no | yes, over TLS |
| [`none`](#none--no-inbound-authentication) | single-user, unauthenticated | no | **no — localhost only** |

### `oidc` (default)

Validates an IdP-issued Bearer token against the IdP's JWKS, then exchanges it for a wger credential (see *How auth works*).

```ini
MCP_AUTH=oidc
OIDC_ISSUER=https://idp.example.com/realms/main   # or https://tenant.auth0.com/
MCP_OIDC_USERNAME_CLAIM=preferred_username  # which claim names the user
#MCP_OIDC_AUDIENCE=wger-mcp                  # if set, inbound aud/azp must match
#MCP_OIDC_ALLOWED_USERS=alice,bob            # optional allowlist

# This server as a confidential OIDC client (token-exchange):
OIDC_CLIENT_ID=wger-mcp
OIDC_CLIENT_SECRET=...
WGER_OIDC_AUDIENCE=wger                      # = wger's OIDC client id at the IdP
WGER_ALLAUTH_PROVIDER=openid_connect         # wger's allauth provider id (slug)
```

JWKS and token endpoints are resolved from the IdP's discovery document (override with `OIDC_JWKS_URI` / `OIDC_TOKEN_ENDPOINT`). Verified on the inbound token: signature (via JWKS), `iss`, `exp`, and — if `MCP_OIDC_AUDIENCE` is set — `aud` (or `azp`, which some IdPs use). JWKS is cached for `MCP_JWKS_TTL_SECONDS` (default 3600 s) and re-fetched on signature failure to handle key rotation.

Interactive MCP clients discover the IdP via OAuth Protected Resource Metadata at `/.well-known/oauth-protected-resource` (a `401` also advertises it in `WWW-Authenticate`). Set `MCP_PUBLIC_URL` to the externally reachable base URL so the advertised resource identifier is correct.

#### Authorization-Server facade

Some MCP clients — notably **claude.ai**'s custom connector — do **not** follow the `authorization_servers` pointer to a separate IdP host. They treat the MCP server's own origin as the OAuth authorization server: they fetch `{origin}/.well-known/oauth-authorization-server` and run `/authorize` + `/token` against that origin. They also need the OAuth endpoints reachable from where the *client* runs — for a cloud client like claude.ai, the public internet — while the IdP itself can stay private.

To support this, the server exposes a thin **AS facade** in `oidc` mode:

| Path | Behaviour |
|------|-----------|
| `/.well-known/oauth-protected-resource` | `authorization_servers` = **this origin** (self) |
| `/.well-known/oauth-authorization-server` | RFC 8414 metadata; `authorization_endpoint`/`token_endpoint` on **this origin** |
| `/authorize` | `302` to the IdP's authorization endpoint (front-channel browser login) |
| `/token` | reverse-proxies to the IdP's token endpoint (back-channel) |

The facade paths default to the conventional `/authorize` and `/token` — clients like claude.ai assume those and ignore the `authorization_endpoint` in the AS metadata. Override with `OAUTH_AUTHORIZE_PATH` / `OAUTH_TOKEN_PATH` if a client expects something else (no rebuild needed).

The IdP (e.g. Keycloak) never has to be publicly reachable: the user's browser reaches it for the login redirect, and the back-channel token request is proxied through this server. Tokens are still minted and signed by the IdP, so inbound validation (`iss` = IdP) is unchanged. The IdP's `authorize`/`token` endpoints come from discovery (override with `OIDC_AUTHORIZATION_ENDPOINT` / `OIDC_TOKEN_ENDPOINT`). See [docs/adr/0003-oauth-authorization-server-facade.md](docs/adr/0003-oauth-authorization-server-facade.md).

`MCP_PUBLIC_URL` **must** be set to the externally reachable base URL so the advertised endpoints point at the public origin (otherwise they're derived from the request's `X-Forwarded-*` / `Host`).

##### Adding the connector in claude.ai

1. At the IdP, the confidential client (`OIDC_CLIENT_ID`) needs redirect URI `https://claude.ai/api/mcp/auth_callback` and web origin `https://claude.ai`, plus *Standard flow* and the token-exchange / audience-mapper setup from *Prerequisites* above.
2. In claude.ai → *Add custom connector*: URL `https://<public-host>/mcp`; under *Advanced settings* set Client ID / secret to the IdP client's.
3. Verify discovery before connecting:
   ```bash
   curl -s https://<public-host>/.well-known/oauth-protected-resource | jq
   curl -s https://<public-host>/.well-known/oauth-authorization-server | jq
   ```

> The interactive `/authorize` step `302`s the browser to the IdP, so the **browser** must reach the IdP. With a split-horizon / LAN-only IdP that means running the browser on that network; the back-channel `/token` is always proxied through this server.

### `static_token` — single-user, no IdP required

For self-hosting where standing up an IdP is overkill. Callers present a shared secret as a bearer token; the server validates it (constant-time) and then calls wger with your personal DRF API key (Settings → API → "API key").

```ini
MCP_AUTH=static_token
MCP_STATIC_TOKEN=<openssl rand -hex 32>
WGER_DEV_TOKEN=<your personal wger API key>
```

The client sends `Authorization: Bearer <MCP_STATIC_TOKEN>`.

Unlike `none`, inbound requests **are** authenticated, so this is safe to expose over TLS. Caveats:

- **Single-user.** Every authenticated caller acts as the one wger account behind `WGER_DEV_TOKEN`. Use `oidc` if more than one person needs access.
- **The secret is a password.** It grants full access to that account; rotate it by changing the env var and restarting.
- **Minimum 32 characters**, enforced at startup — a guessable secret is the entire attack surface.
- **No MCP-native OAuth.** The OAuth discovery endpoints are deliberately not served under this strategy (a client following them would run a flow whose token this server never accepts), so configure the token out-of-band in your client.

### `none` — no inbound authentication

**Anyone who can reach `/mcp` acts as the account behind `WGER_DEV_TOKEN`** — no credential required. The server logs a warning at startup.

```ini
MCP_AUTH=none
WGER_DEV_TOKEN=<your personal wger API key>
```

Safe only when bound to localhost for local development. Never expose it to a network, even behind TLS — use `static_token` instead if you need remote access.

## Tools

Tools are grouped by domain. Each lives in its own module under [`src/wger_mcp/tools/`](src/wger_mcp/tools/).

### Registering only some groups

All 81 tools are registered by default. `MCP_TOOLS` narrows that to a comma-separated list of groups:

```bash
MCP_TOOLS=nutrition,off,profile     # a food-logging agent
```

Valid group names are the module names: `profile`, `routines`, `workout_logs`, `body_weight`, `measurements`, `equipment`, `nutrition`, `exercises`, `analytics`, `off`. An unknown name stops the server at startup rather than silently dropping tools, and repeated names are harmless.

This matters most for agents driven by small local models, whose tool-selection accuracy falls off as the surface grows, and where every schema is spent from a modest context window. It is also useful for a single-purpose agent that has no business writing routines.

### Profile

| Tool | Description |
|------|-------------|
| `whoami` | Show the wger user profile of the authenticated caller |
| `update_user_profile(calories?, height_cm?, birthdate?, gender?, sleep_hours?, work_hours?, work_intensity?, sport_hours?, sport_intensity?, freetime_hours?, freetime_intensity?)` | Patch the wger profile (e.g. write your calorie target) |

### Routines (training plan tree)

| Tool | Description |
|------|-------------|
| `list_routines` / `get_routine(routine_id)` | List / read training routines |
| `create_routine(name, description?, start?, end?, fit_in_week?)` | Create a routine |
| `update_routine(routine_id, ...)` / `delete_routine(routine_id)` | Patch / delete a routine (cascade) |
| `list_routine_days(routine_id)` / `get_routine_day(day_id)` | Read day structure |
| `add_routine_day(routine_id, name, order, description?, is_rest?, day_type?)` | Add a training day |
| `update_routine_day(day_id, ...)` / `delete_routine_day(day_id)` | Patch / delete a day (cascade) |
| `list_slots(day_id)` / `add_slot_to_day(day_id, order, sets?, rest_seconds?)` | List / add exercise slots |
| `update_slot(slot_id, ...)` / `delete_slot(slot_id)` | Patch / delete a slot (cascade) |
| `list_slot_entries(slot_id)` / `get_slot_entry(entry_id)` | Read exercise entries in a slot |
| `attach_exercise_to_slot(slot_id, exercise_id, order?, repetition_unit?, weight_unit?, comment?)` | Bind an exercise to a slot |
| `update_slot_entry(entry_id, ...)` / `delete_slot_entry(entry_id)` | Patch / delete a slot entry |
| `list_slot_entry_configs(slot_entry_id, kinds?)` | Read per-iteration configs (sets/reps/weight/rir/rest/max_*) |
| `set_slot_entry_config(slot_entry_id, kind, value, iteration?, operation?, step?, repeat?)` | Add a per-iteration config record |
| `update_slot_entry_config(kind, config_id, value?, iteration?, ...)` / `delete_slot_entry_config(kind, config_id)` | Patch / delete a config record (use to bump weight on progression) |
| `add_exercise_with_sets(day_id, exercise_id, sets, reps, weight_kg, slot_order?, rest_seconds?)` | Convenience: slot + entry + sets/reps/weight configs in one call |
| `list_workouts` | Legacy workout plans |

### Workout logs

| Tool | Description |
|------|-------------|
| `log_set(exercise_id, reps, weight_kg, workout_log_date?, rir?)` | Add a workout log entry |
| `list_workout_logs(date_from?, date_to?, exercise_id?, limit?)` / `get_workout_log(log_id)` | Read entries |
| `update_workout_log(log_id, ...)` / `delete_workout_log(log_id)` | Edit / remove an entry |

### Body weight

| Tool | Description |
|------|-------------|
| `log_body_weight(weight_kg, when?)` | Body-weight entry |
| `get_body_weight_history(limit?)` | Recent weight entries |
| `update_body_weight_entry(entry_id, ...)` / `delete_body_weight_entry(entry_id)` | Edit / remove an entry |

### Exercise catalog

| Tool | Description |
|------|-------------|
| `search_exercises(query, language, limit)` | Find exercises by name (ISO 639-1 language code) |
| `search_exercises_by_filter(equipment_id?, muscle_id?, category_id?, language?, limit?)` | Structured lookup (e.g. Dumbbell + Back) |
| `get_exercise(id)` | Full exercise detail: muscles, equipment, instructions, images (with 2.6 `small`/`medium` thumbnails) |
| `list_categories` / `list_equipment` / `list_muscles` | Reference data |

### Ingredients

| Tool | Description |
|------|-------------|
| `search_ingredients(query, language, limit, nutriscore?, nutriscore_better_than?, nutriscore_at_worst?)` | Find foods by name. Optional Nutri-Score filters (wger 2.6): exact grade, or `nutriscore_better_than='C'` (A/B only), or `nutriscore_at_worst='C'` (C or better) |
| `search_ingredient_by_barcode(barcode, limit?)` | Exact lookup by EAN/UPC (`?code=`) — preferred over name search |
| `get_ingredient(ingredient_id)` | Full ingredient detail (macros per 100 g) |

> wger's REST `/ingredient/` is **read-only** by design (community-maintained DB), so there is no `create_ingredient` tool. Submitting custom ingredients previously drove wger's Django web form with username/password; that path was dropped with the move to multi-user SSO auth.

### Nutrition plans, meals, recipes, diary

| Tool | Description |
|------|-------------|
| `list_nutrition_plans` / `get_nutrition_plan(plan_id)` | Read nutrition plans |
| `create_nutrition_plan(description?, only_logging?, goal_energy?, goal_protein?, goal_carbohydrates?, goal_fat?)` | Create a plan (returns `plan_id`) |
| `update_nutrition_plan(plan_id, ...)` / `delete_nutrition_plan(plan_id)` | Patch / delete a plan (cascade) |
| `create_meal(plan_id, name, order?, time?)` | Add a meal to a plan |
| `create_recipe(plan_id, name, order?)` / `get_recipe(recipe_id)` / `add_ingredient_to_recipe(recipe_id, ingredient_id, amount_g, order?, weight_unit_id?)` | Recipes (semantic aliases over `meal/` + `mealitem/` — wger has no separate Recipe entity) |
| `log_ingredient(plan_id, ingredient_id, amount_g, when?, meal_id?)` | Nutrition diary entry. `when` takes a full timestamp (`2026-07-21T07:30:00+02:00`, offset preserved) or a bare date (anchored at 12:00); omit it to let wger use the current time |
| `update_log_item(log_item_id, amount_g?, when?, ingredient_id?, meal_id?)` | Patch a diary entry — the way to correct an entry's time or amount in place |
| `list_log_items(when?, plan_id?, limit?)` / `delete_log_item(log_item_id)` | List / remove diary entries |
| `nutrition_summary(when?, plan_id?)` | Daily kcal/protein/carbs/fat from diary entries |
| `calculate_daily_calories(weight_kg?, height_cm?, age?, sex?, activity_level?, goal?, protein_g_per_kg?, fat_pct_of_kcal?, apply_to_profile?)` | Mifflin-St Jeor TDEE + macro split. All physical inputs auto-fill from `userprofile/` + latest `weightentry/`. `apply_to_profile=True` PATCHes the result into `userprofile.calories` |

### Analytics

| Tool | Description |
|------|-------------|
| `weekly_summary(days?)` | Aggregate workoutlog: sets, reps, volume per exercise |
| `exercise_history(exercise_id, days?, limit?)` | Per-session aggregates (sets, reps, top weight, volume) for one exercise |
| `personal_records(exercise_id?, days?)` | Max weight, max reps, Epley-estimated 1RM per exercise |
| `volume_trend(days?, bucket, metrics?, group_by?, exercise_id?)` | Bucketed (day/week/month) volume; group_by none/exercise/muscle/category |
| `compare_periods(window_days?, gap_days?, metrics?, group_by?)` | Rolling window A vs B (delta + delta%) |

### Open Food Facts (external food database)

| Tool | Description |
|------|-------------|
| `lookup_food_by_barcode(barcode, language?)` | Resolve an EAN/UPC/GTIN on Open Food Facts. Returns the localised name + ingredients (when present), macros per 100 g, and a normalised `wger_ingredient_payload` (informational). Salt→sodium conversion applied automatically |
| `lookup_foods_by_barcodes(barcodes[], language?)` | Batch variant — concurrent fetches (capped at 4 in flight) with one-shot retry on 429. Returns map keyed by barcode |

> Use these when you have a barcode — far more accurate than wger name search. Coverage is good for branded packaged goods and thin for supermarket private-labels, and it varies a lot by country. For items missing on OFF, the response includes a `suggestion` URL to add them — additions flow back into wger via the next ingredient-sync.
>
> **Language.** OFF stores per-language fields (`product_name_<lang>`, `ingredients_text_<lang>`). Which one is requested and preferred comes from `DEFAULT_LANGUAGE` (default `en`), and every tool with a `language` argument overrides it per call. The response echoes the resolved `language` and carries both `name_localized` and the language-neutral `name_default`.

## Configuring a client

### Interactive (MCP-native OAuth)

Point the client at the Streamable HTTP URL. On first use it fetches
`/.well-known/oauth-protected-resource`, runs the OAuth flow against the IdP,
and attaches the resulting Bearer token automatically.

```json
{
  "mcpServers": {
    "wger": {
      "type": "streamable-http",
      "url": "https://wger-mcp.example.com/mcp"
    }
  }
}
```

### Scripts / headless (manual Bearer)

Obtain an OIDC token out-of-band and pass it as `Authorization: Bearer <token>`.
See `scripts/get_token.py` for a device-flow example. The token's
audience must be acceptable to the server (`MCP_OIDC_AUDIENCE`); the server then
exchanges it for a wger credential.

## Deployment

CI publishes a multi-arch image to the GitHub Container Registry on every push to the default branch and on `v*.*.*` tags:

```bash
docker pull ghcr.io/wger-project/mcp-server:latest
```

A reference Docker setup ships in `Dockerfile` and `compose.example.yml`. The server is a single ASGI app (`wger_mcp.server:build_app`) and can also be run under any ASGI host (Hypercorn, Granian, gunicorn-uvicorn, …).

If exposed over HTTPS via a reverse proxy, configure the proxy with:

```nginx
proxy_buffering off;
proxy_request_buffering off;
proxy_read_timeout 3600s;
```

so that streamable-HTTP/SSE responses aren't buffered.

## Documentation

- [CONTRIBUTING.md](CONTRIBUTING.md) — development setup, project layout, how to add a tool or an auth strategy.
- [docs/api-keys.md](docs/api-keys.md) — which credential goes where, and a `401` troubleshooting table.
- [CONTEXT.md](CONTEXT.md) — glossary of the terms used across these docs.
- [docs/HANDOFF.md](docs/HANDOFF.md) — maintainer notes: non-obvious constraints, open items, and known limitations.
- [docs/adr/](docs/adr/) — architecture decisions and the reasoning behind them:
  - [0001](docs/adr/0001-multi-user-auth-via-oidc-token-exchange.md) — multi-user access via OIDC token exchange
  - [0002](docs/adr/0002-opaque-string-resource-ids.md) — opaque string resource ids
  - [0003](docs/adr/0003-oauth-authorization-server-facade.md) — the OAuth authorization-server facade
  - [0004](docs/adr/0004-static-token-strategy-for-single-user.md) — the `static_token` strategy

## Development

```bash
uv sync --dev
uv run pytest        # inbound auth (OIDC + static token), token exchange, wger client, tools
uv run ruff check .
```

### Source layout

- [`src/wger_mcp/server.py`](src/wger_mcp/server.py) — Starlette + FastMCP wiring, lifespan, healthcheck, OAuth metadata, auth middleware.
- [`src/wger_mcp/wger_client.py`](src/wger_mcp/wger_client.py) — async httpx wrapper. Resolves the per-request wger credential from the token provider. `paginate()` uses `count` + `next` URL to fan out remaining pages concurrently (page- or offset-style), with serial fallback for unknown formats.
- [`src/wger_mcp/auth/`](src/wger_mcp/auth/) — inbound OIDC validation (`oidc.py`, discovery in `oidc_discovery.py`), token exchange + outbound credential provider (`exchange.py`), per-request identity (`identity.py`), OAuth metadata (`oauth.py`).
- [`src/wger_mcp/tools/`](src/wger_mcp/tools/) — one module per domain. Each exposes `register(mcp, client, settings)`; [`tools/__init__.py`](src/wger_mcp/tools/__init__.py) registers them all.

### Performance notes

- `nutrition_summary`, `list_slot_entry_configs`, `_load_ex_meta` (used by `volume_trend` / `compare_periods`) fan out per-id fetches via `asyncio.gather` + a small `Semaphore`. Concurrency caps live in the tool modules — tune down if your wger instance applies per-token rate limits.
- Exercise metadata is cached process-wide (`_EX_META_CACHE` in [`tools/analytics.py`](src/wger_mcp/tools/analytics.py)) — analytics tools called repeatedly within one process pay the metadata cost only once.
- `compare_periods` issues two range queries in parallel and skips fetching the gap window entirely.

## Upgrading

### Language is now configurable (was hard-coded Polish)

The Open Food Facts tools used to always request the Polish fields
(`product_name_pl`, `ingredients_text_pl`), and the exercise/ingredient search
tools defaulted to `en`. Both now follow `DEFAULT_LANGUAGE` (default `en`), with
a per-call `language` argument overriding it.

- **To keep the previous OFF behaviour**, set `DEFAULT_LANGUAGE=pl`. Otherwise
  barcode lookups return English names where a Polish one used to be preferred.
- **Response keys changed** on `lookup_food_by_barcode` /
  `lookup_foods_by_barcodes`: `name_pl` → `name_localized`,
  `ingredients_text_pl` → `ingredients_text`, plus a new `language` field
  echoing the resolved code. `name`, `name_default` and the macro fields are
  unchanged.

## License

AGPL-3.0-or-later, matching the wger project. See [LICENSE](LICENSE).
