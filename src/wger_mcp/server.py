"""FastMCP server: wger tools over stdio or streamable HTTP.

Tool implementations live in ``wger_mcp.tools``; this module only wires the
FastMCP instance, the upstream HTTP client, and whichever transport is running.

:func:`build_server` does the transport-neutral half — it is all either
transport needs, and each caller hands it the FastMCP options its own transport
has. :func:`build_app` wraps the result in Starlette for HTTP, adding the
inbound auth middleware and the OAuth endpoints; :func:`serve_stdio` just pumps
the same FastMCP instance over stdin/stdout.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from dataclasses import dataclass
from typing import Any

import anyio
import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from wger_api_client.client import AuthenticatedClient

from . import __version__
from .api_client import build_api_client
from .auth import (
    AS_METADATA_PATH,
    WELL_KNOWN_PATH,
    WgerTokenProvider,
    build_auth_middleware,
    build_authorization_server_facade,
    build_token_provider,
    forwarded_origin,
    protected_resource_metadata,
    resource_identifier,
)
from .config import (
    AuthStrategy,
    ConfigError,
    Settings,
    Transport,
    env_file_for,
    load_settings,
    resolve_transport,
    transport_declared_in,
    transport_in_environ,
)
from .tools import off, register_all

log = logging.getLogger("wger_mcp")


@dataclass(frozen=True)
class Server:
    """A configured FastMCP instance plus the clients whose lifetime it shares."""

    mcp: FastMCP
    api: AuthenticatedClient
    off_http: httpx.AsyncClient
    provider: WgerTokenProvider

    async def aclose(self) -> None:
        await self.api.get_async_httpx_client().aclose()
        await self.off_http.aclose()
        await self.provider.aclose()


def build_server(settings: Settings, **fastmcp_kwargs: Any) -> Server:
    """Wire up FastMCP and the upstream clients — identical for both transports.

    Options that only one transport has are the caller's to pass, because only
    the caller knows which one it is; the tool modules see nothing but the API
    client, so nothing here needs to ask whether it will be driven over a pipe
    or over HTTP.
    """
    mcp = FastMCP("wger", **fastmcp_kwargs)

    provider = build_token_provider(settings)
    api = build_api_client(settings, provider)
    off_http = off.build_http()
    register_all(mcp, api, off_http, settings)
    return Server(mcp=mcp, api=api, off_http=off_http, provider=provider)


def _require_transport(settings: Settings, expected: Transport, entry_point: str) -> None:
    """Refuse settings meant for the other transport.

    Half the configuration is transport-specific — the auth strategy, the mount
    path, the host allow-list — and the validator relaxes rules for stdio that
    HTTP relies on. Building one from the other's settings therefore does not
    fail, it succeeds into something misconfigured, so say so instead.
    """
    if settings.mcp_transport is not expected:
        raise ValueError(
            f"{entry_point} needs MCP_TRANSPORT={expected.value}, got "
            f"{settings.mcp_transport.value}. Settings for the other transport are "
            "not interchangeable: under stdio there is no inbound authentication, "
            "so MCP_AUTH is forced to 'none' and MCP_PATH and ALLOWED_HOSTS are "
            "never applied."
        )


async def serve_stdio(settings: Settings) -> None:
    """Serve MCP over stdin/stdout until the client closes the pipe.

    stdout belongs to the JSON-RPC framing from here on; anything else written
    there corrupts the stream (logging goes to stderr, see :func:`main`).
    """
    _require_transport(settings, Transport.stdio, "serve_stdio()")
    server = build_server(settings)
    try:
        await server.mcp.run_stdio_async()
    finally:
        await server.aclose()


def build_app(settings: Settings) -> Starlette:
    """The ASGI application, for uvicorn or any other host.

    Documented as an entry point (``wger_mcp.server:build_app``), so it is the
    place a leftover ``MCP_TRANSPORT=stdio`` in an operator's environment would
    otherwise land — and produce an app with no inbound auth, on the wrong path,
    with the host allow-list dropped. Rejected up front instead.
    """
    _require_transport(settings, Transport.http, "build_app()")
    server = build_server(
        settings,
        json_response=True,
        streamable_http_path=settings.mcp_path,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=bool(settings.allowed_hosts),
            allowed_hosts=settings.allowed_hosts,
        ),
    )
    mcp = server.mcp

    # AS facade: lets a client that treats this origin as the OAuth authorization
    # server (e.g. claude.ai) reach a private IdP. None when not in OIDC mode.
    as_facade = build_authorization_server_facade(settings)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp.session_manager.run():
            try:
                yield
            finally:
                await server.aclose()
                if as_facade is not None:
                    await as_facade.aclose()

    async def healthcheck(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    async def oauth_metadata(request: Request) -> JSONResponse:
        origin = forwarded_origin(request)
        return JSONResponse(protected_resource_metadata(settings, origin=origin))

    async def as_metadata(request: Request) -> JSONResponse:
        origin = resource_identifier(settings, origin=forwarded_origin(request))
        return JSONResponse(as_facade.metadata(origin))

    # streamable_http_app() registers Route(mcp_path, ...) internally.
    # Merging its routes into the top-level Starlette avoids the double-prefix
    # problem: an outer Mount("/mcp/") would strip the prefix before routing,
    # leaving "" which never matches the inner Route("/mcp/") → 404.
    # For every MCP route we also register its slash-twin (the same path with the
    # trailing "/" toggled) so `/mcp` and `/mcp/` both hit the ASGI app no matter
    # how MCP_PATH is written. MCP clients (and curl) do not follow the 307
    # redirect_slashes would otherwise emit on POST, so a twin is required rather
    # than a redirect.
    mcp_starlette = mcp.streamable_http_app()
    mcp_routes: list[Route] = []
    seen_paths: set[str] = set()
    for route in mcp_starlette.routes:
        mcp_routes.append(route)
        path = getattr(route, "path", None)
        if path:
            seen_paths.add(path)
    for route in list(mcp_routes):
        path = getattr(route, "path", None)
        endpoint = getattr(route, "endpoint", None) or getattr(route, "app", None)
        if not path or not endpoint:
            continue
        twin = path[:-1] if path.endswith("/") else path + "/"
        if twin and twin not in seen_paths:
            mcp_routes.append(Route(twin, endpoint))
            seen_paths.add(twin)
    routes = [Route("/health", healthcheck), *mcp_routes]
    # OAuth-protected-resource metadata lets interactive MCP clients discover
    # the SSO IdP as the authorization server. Only meaningful when OIDC is the
    # inbound strategy: advertising it under static_token/none would send
    # clients through an OAuth flow whose result the server never accepts.
    if settings.mcp_auth is AuthStrategy.oidc and settings.oidc_issuer is not None:
        routes.append(Route(WELL_KNOWN_PATH, oauth_metadata))
        if as_facade is not None:
            routes.append(Route(AS_METADATA_PATH, as_metadata))
            routes.append(
                Route(settings.oauth_authorize_path, as_facade.authorize, methods=["GET"])
            )
            routes.append(Route(settings.oauth_token_path, as_facade.token, methods=["POST"]))
    app = Starlette(routes=routes, lifespan=lifespan)
    app.router.redirect_slashes = False
    auth_cls, auth_kwargs = build_auth_middleware(settings)
    app.add_middleware(auth_cls, **auth_kwargs)
    return app


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wger-mcp",
        description="MCP server for the wger fitness/nutrition API.",
    )
    parser.add_argument(
        "--transport",
        choices=[t.value for t in Transport],
        default=None,
        help=(
            "stdio: the MCP client spawns this process and talks over a pipe "
            "(local, single-user, needs only WGER_BASE_URL and WGER_API_KEY). "
            "http: listen on HOST:PORT with the MCP_AUTH strategy. "
            "Defaults to $MCP_TRANSPORT, else http."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=None,
        metavar="PATH",
        help=(
            "Read settings from this file; it must exist. Defaults to ./.env for "
            "http (optional), and to nothing for stdio, where the working "
            "directory is the client's."
        ),
    )
    parser.add_argument("--version", action="version", version=f"wger-mcp {__version__}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    # stderr, always: under stdio, stdout carries the JSON-RPC stream and a log
    # line landing there would break the client's parser.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # The transport decides whether an env file is read at all, so it is resolved
    # from the command line and the environment only — never from such a file.
    # A bad transport name or a missing --env-file is the operator's typo, not a
    # bug: report the sentence, not a traceback.
    try:
        transport = resolve_transport(args.transport)
        env_file = env_file_for(transport, args.env_file)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc

    # The flag beating the environment is by design (the Docker image pins it
    # that way), but an operator who set the variable deserves to hear that it
    # did nothing. Suppressed on a bogus value: that is the flag's job to
    # override, not a reason to refuse to start.
    if args.transport:
        with contextlib.suppress(ValueError):
            from_env = transport_in_environ()
            if from_env is not None and from_env is not transport:
                log.warning(
                    "--transport %s overrides MCP_TRANSPORT=%s from the environment",
                    transport.value,
                    from_env.value,
                )

    # An env file cannot set the transport — refuse rather than pick one of the
    # two bad answers: honouring it means stdio configured out of a file stdio
    # promises to ignore, and ignoring it means the line silently does nothing.
    # Checked before loading so this reports the cause, rather than whatever the
    # rest of the file happens to be invalid for under the other transport.
    declared = transport_declared_in(env_file)
    if declared is not None and declared != transport.value:
        raise SystemExit(
            f"MCP_TRANSPORT={declared} is set in {env_file}, but the transport is "
            f"what decides whether that file is read at all, so it cannot come "
            f"from there. Pass --transport {declared} or set MCP_TRANSPORT in the "
            "environment instead."
        )

    # Passed as an override so the resolution above stays the only answer.
    try:
        settings = load_settings(env_file=env_file, mcp_transport=transport)
    except ConfigError as exc:
        # Same shape as the transport errors above: the operator's typo deserves
        # one readable line, not a traceback.
        raise SystemExit(str(exc)) from None

    if settings.mcp_transport is Transport.stdio:
        log.info("transport=stdio, wger=%s", settings.wger_base_url)
        try:
            anyio.run(serve_stdio, settings)
        except KeyboardInterrupt:
            # Ctrl-C, or a client stopping the server it spawned: the ordinary
            # end of service, not a crash. asyncio's runner re-raises it after
            # cancelling, so serve_stdio's cleanup has already run by now.
            # uvicorn does the same on the http side; without this the client's
            # log pane collects a traceback on every stop.
            log.info("interrupted, shutting down")
        return

    log.info("MCP_AUTH=%s, MCP_PATH=%s", settings.mcp_auth.value, settings.mcp_path)
    app = build_app(settings)
    # forwarded_allow_ips="*" so uvicorn trusts X-Forwarded-Proto / -For from any
    # peer. Required when running behind a reverse proxy on a separate IP (the
    # default whitelist of 127.0.0.1 silently ignores headers from nginx etc).
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
