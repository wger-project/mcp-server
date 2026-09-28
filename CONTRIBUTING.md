# Contributing

Thanks for your interest! This is the MCP server for [wger](https://wger.de);
it talks to a wger instance over its public REST API and needs no changes to
wger itself.

Bug reports and pull requests are welcome at
<https://github.com/wger-project/mcp-server>. For questions about wger itself,
see the [main wger repository](https://github.com/wger-project/wger).

## Development setup

Requires Python >= 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --dev
uv run pytest        # full suite
uv run ruff check .
```

To run the server against a real wger instance, copy `.env.example` to `.env`
and use `MCP_AUTH=static_token` — it needs only a wger API key, no IdP. See the
[README](README.md#static_token--single-user-no-idp-required).

## Project layout

```
src/wger_mcp/
├── server.py          # Starlette + FastMCP wiring, routes, lifespan
├── wger_client.py     # async httpx wrapper around the wger REST API
├── config.py          # pydantic-settings; auth strategy selector
├── auth/
│   ├── __init__.py    # build_auth_middleware / build_token_provider factories
│   ├── base.py        # shared helpers, NoAuthMiddleware, StaticTokenMiddleware
│   ├── wger_oidc.py   # inbound: wger's own opaque token, carried through
│   ├── oidc.py        # inbound OIDC token validation against the IdP's JWKS
│   ├── oidc_discovery.py  # resolves the provider's endpoints from its issuer
│   ├── exchange.py    # outbound credential: pass-through, exchange or API key
│   ├── asfacade.py    # OAuth authorization-server facade (see ADR 0003)
│   ├── identity.py    # per-request identity (contextvar)
│   └── oauth.py       # OAuth protected-resource metadata
└── tools/             # one module per domain, see below
```

Architecture decisions live in [`docs/adr/`](docs/adr/). Read those before
changing how auth works.

## Adding a new tool

Tools live in `src/wger_mcp/tools/`, one module per domain. Each module exposes
`register(mcp, client, settings)` and is listed in `tools/__init__.py`.

Within a module, each tool is an `async def` decorated with `@mcp.tool()` and
type-annotated parameters — FastMCP turns these into the MCP tool schema
automatically, and the docstring becomes the description the model sees, so
write it for that audience.

Wrap upstream calls in `try/except WgerError` and return `err(exc)` (from
`tools/common.py`) so failures reach the client as a structured payload rather
than an exception.

Prefer `client.paginate()` over manual page walking; it fans out remaining
pages concurrently.

## Adding a new auth strategy

1. Add a value to the `AuthStrategy` enum in `config.py`.
2. Add its settings to `Settings`, and validate them in
   `_check_strategy_requirements` so misconfiguration fails at startup rather
   than on the first request.
3. Add a middleware class (in `auth/base.py` for simple ones, or its own
   module) that:
   - bypasses public paths via `is_bypass_path`
   - calls `set_identity(...)` on success
   - calls `reply_unauthorized(...)` on failure
4. Wire it into the `build_auth_middleware` factory in `auth/__init__.py`, and
   `build_token_provider` if it changes how the outbound wger credential is
   obtained.
5. Check whether the OAuth discovery routes in `server.py` should be served
   under the new strategy — advertising them when the server will not accept
   the resulting tokens sends clients through a flow that cannot succeed.
6. Add tests under `tests/test_auth_<name>.py`.

## Tests

- Tests run via Starlette's `TestClient`; `make_client(**env)` in `conftest.py`
  rebuilds the app under the given environment.
- `respx` mocks outbound HTTP — both wger API calls and JWKS fetches.
- The MCP `initialize` request is the cheapest way to exercise the full
  middleware chain end-to-end.
- Use `example.com` hosts and obviously-fake credentials in fixtures.

## License

AGPL-3.0-or-later, matching the wger project. By contributing you agree your
contributions are licensed under the same terms. See [LICENSE](LICENSE).
