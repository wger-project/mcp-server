"""The stdio transport, exercised as a real client would: a spawned subprocess.

These tests run ``wger-mcp --transport stdio`` out-of-process on purpose. The
things that break stdio servers — a log line on stdout, a ``.env`` picked up
from whatever directory the client happened to start in — only exist across a
process boundary, so an in-process test would not see them.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import ValidationError
from starlette.testclient import TestClient

from wger_mcp.config import (
    DEFAULT_ENV_FILE,
    AuthStrategy,
    Settings,
    Transport,
    env_file_for,
    load_settings,
    resolve_transport,
    transport_declared_in,
)
from wger_mcp.server import build_app, serve_stdio

from .conftest import scrubbed_env

# Enough to satisfy config validation; no request ever reaches this host.
STDIO_ENV = {
    "WGER_BASE_URL": "https://wger.test",
    "WGER_API_KEY": "test-api-key",
}

_TIMEOUT = 30


def _env(**extra: str) -> dict[str, str]:
    """The subprocess environment, scrubbed by the same rule as the fixture.

    A subprocess inherits the real environment, so the autouse fixture cannot
    help here — but the definition of "a settings variable" must not fork.
    """
    return scrubbed_env(**(STDIO_ENV | extra))


def _params(cwd: Path | None = None, **extra: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "wger_mcp.server", "--transport", "stdio"],
        env=_env(**extra),
        cwd=str(cwd) if cwd else None,
    )


@asynccontextmanager
async def _session(params: StdioServerParameters) -> AsyncIterator[ClientSession]:
    """A session over a spawned server, on a deadline.

    A dead child is harmless — the SDK turns that into CONNECTION_CLOSED right
    away. The mode that needs the deadline is a server that is up and never
    answers, which is the very failure these tests exist to catch ("initialize()
    would never complete", below). Without it such a run does not fail, it hangs:
    there is no per-test timeout here, so CI would sit until the job limit,
    across the whole Python matrix, instead of reporting in half a minute.
    """
    with anyio.fail_after(_TIMEOUT):
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            yield session


async def test_initializes_and_lists_tools() -> None:
    async with _session(_params()) as session:
        result = await session.initialize()
        assert result.serverInfo.name == "wger"
        tools = await session.list_tools()

    names = {t.name for t in tools.tools}
    assert "whoami" in names, names
    assert len(names) > 20


async def test_tool_selection_still_applies() -> None:
    """MCP_TOOLS is transport-neutral — verify it survives the stdio path."""
    async with _session(_params(MCP_TOOLS="body_weight")) as session:
        await session.initialize()
        tools = await session.list_tools()

    names = {t.name for t in tools.tools}
    assert names
    assert all("weight" in n or "bodyweight" in n.lower() for n in names), names


async def test_a_stray_env_file_in_the_cwd_is_ignored(tmp_path: Path) -> None:
    """The CWD belongs to the client, so ./.env must not configure the server.

    The poison file sets MCP_AUTH=oidc, which stdio rejects at startup — if the
    file were read, initialize() would never complete.
    """
    (tmp_path / ".env").write_text("MCP_AUTH=oidc\nWGER_BASE_URL=https://poisoned.test\n")

    async with _session(_params(cwd=tmp_path)) as session:
        result = await session.initialize()

    assert result.serverInfo.name == "wger"


async def test_a_lowercase_env_var_selects_stdio_and_still_ignores_the_file(
    tmp_path: Path,
) -> None:
    """pydantic-settings matches env vars case-insensitively, so we must too.

    A case-sensitive lookup would resolve 'http' here, read the file on that
    basis, and then serve stdio anyway — configured out of it.
    """
    (tmp_path / ".env").write_text("MCP_AUTH=oidc\nWGER_BASE_URL=https://poisoned.test\n")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "wger_mcp.server"],  # no flag: the variable has to carry it
        env=_env(mcp_transport="stdio"),
        cwd=str(tmp_path),
    )

    async with _session(params) as session:
        result = await session.initialize()

    assert result.serverInfo.name == "wger"


def test_an_env_file_may_not_select_stdio(tmp_path: Path) -> None:
    """Honouring it contradicts the docs, ignoring it is silent — so: refuse.

    The message has to name the real cause. Before, a file that also set
    MCP_AUTH=oidc (the uncommented default in .env.example) died on the oidc
    refusal instead, pointing at the wrong line.
    """
    (tmp_path / ".env").write_text(
        "MCP_TRANSPORT=stdio\nMCP_AUTH=oidc\n"
        "WGER_BASE_URL=https://from-dotenv.test\nWGER_API_KEY=k\n"
    )
    env = {k: v for k, v in _env().items() if k not in ("WGER_BASE_URL", "WGER_API_KEY")}
    proc = subprocess.run(
        [sys.executable, "-m", "wger_mcp.server"],
        input="",
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=_TIMEOUT,
    )

    assert proc.returncode != 0
    assert "MCP_TRANSPORT=stdio is set in .env" in proc.stderr
    assert "cannot use MCP_AUTH=oidc" not in proc.stderr


def test_stdout_carries_nothing_but_jsonrpc() -> None:
    """Logging, warnings and startup chatter must all land on stderr.

    A single stray write to stdout desyncs the client's framing, and the failure
    surfaces far from its cause — hence a direct assertion on the raw stream.
    """
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "raw-test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "wger_mcp.server", "--transport", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(),
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    watchdog = threading.Timer(_TIMEOUT, proc.kill)
    watchdog.start()

    lines: list[str] = []
    try:
        for request in requests:
            proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()

        expected_replies = sum(1 for r in requests if "id" in r)
        while len(lines) < expected_replies:
            line = proc.stdout.readline()
            if not line:
                break
            if line.strip():
                lines.append(line)
    finally:
        watchdog.cancel()
        proc.stdin.close()
        stderr = proc.stderr.read()
        proc.wait(timeout=_TIMEOUT)

    assert lines, f"no responses on stdout; stderr was:\n{stderr}"
    for line in lines:
        message = json.loads(line)  # raises if anything non-JSON slipped in
        assert message["jsonrpc"] == "2.0"

    ids = [m.get("id") for m in (json.loads(ln) for ln in lines)]
    assert ids == [1, 2], f"stderr was:\n{stderr}"
    # The startup line proves logging is configured and pointed away from stdout.
    assert "transport=stdio" in stderr


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"MCP_AUTH": "oidc"}, "cannot use MCP_AUTH=oidc"),
        # Was silently rewritten to none, taking the minimum-length check on the
        # secret with it — the same config http refuses outright.
        (
            {"MCP_AUTH": "static_token", "MCP_STATIC_TOKEN": "short"},
            "cannot use MCP_AUTH=static_token",
        ),
        ({"WGER_API_KEY": ""}, "requires WGER_API_KEY"),
    ],
)
def test_misconfiguration_fails_loudly_at_startup(env: dict[str, str], expected: str) -> None:
    """Better a dead process with a readable reason than tools that 401 later."""
    proc = subprocess.run(
        [sys.executable, "-m", "wger_mcp.server", "--transport", "stdio"],
        input="",
        capture_output=True,
        text=True,
        env=_env(**env),
        timeout=_TIMEOUT,
    )

    assert proc.returncode != 0
    assert expected in proc.stderr


class TestTransportResolution:
    """The policy itself, away from the subprocess: one answer, one place."""

    def test_the_command_line_wins_over_the_environment(self) -> None:
        assert resolve_transport("http", {"MCP_TRANSPORT": "stdio"}) is Transport.http

    def test_the_environment_is_matched_case_insensitively(self) -> None:
        assert resolve_transport(None, {"mcp_transport": "stdio"}) is Transport.stdio
        assert resolve_transport(None, {"MCP_TRANSPORT": "StDiO"}) is Transport.stdio

    def test_it_defaults_to_http(self) -> None:
        assert resolve_transport(None, {}) is Transport.http

    def test_a_bogus_value_is_named_in_the_error(self) -> None:
        with pytest.raises(ValueError, match="MCP_TRANSPORT must be one of"):
            resolve_transport(None, {"MCP_TRANSPORT": "pipe"})

    def test_stdio_reads_no_file_unless_one_is_named(self, tmp_path: Path) -> None:
        named = tmp_path / "wger.env"
        named.write_text("WGER_API_KEY=k\n")
        assert env_file_for(Transport.stdio) is None
        assert env_file_for(Transport.stdio, str(named)) == str(named)
        assert env_file_for(Transport.http) == DEFAULT_ENV_FILE

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("MCP_TRANSPORT=stdio", "stdio"),
            ("export MCP_TRANSPORT='stdio'", "stdio"),
            ("  mcp_transport = HTTP ", "http"),
            ("#MCP_TRANSPORT=stdio", None),
            ("WGER_BASE_URL=https://wger.test", None),
        ],
    )
    def test_it_spots_a_transport_in_a_file(
        self, tmp_path: Path, line: str, expected: str | None
    ) -> None:
        path = tmp_path / ".env"
        path.write_text(f"WGER_API_KEY=k\n{line}\n")
        assert transport_declared_in(str(path)) == expected

    def test_a_missing_file_declares_nothing(self, tmp_path: Path) -> None:
        assert transport_declared_in(str(tmp_path / "absent.env")) is None
        assert transport_declared_in(None) is None


class TestTransportMismatch:
    """Neither entry point may accept the other transport's settings."""

    @staticmethod
    def _settings(transport: str) -> Settings:
        return Settings(  # type: ignore[call-arg]
            wger_base_url="https://wger.test",
            mcp_transport=transport,
            mcp_auth="none",
            wger_dev_token="personal-key",
            allowed_hosts=["mcp.example.com"],
            mcp_path="/wger-mcp",
        )

    def test_build_app_refuses_stdio_settings(self) -> None:
        """Otherwise it builds a NoAuthMiddleware app that ignores MCP_PATH and
        ALLOWED_HOSTS — misconfigured rather than broken, which is worse."""
        with pytest.raises(ValueError, match=r"build_app\(\) needs MCP_TRANSPORT=http"):
            build_app(self._settings("stdio"))

    async def test_serve_stdio_refuses_http_settings(self) -> None:
        with pytest.raises(ValueError, match=r"serve_stdio\(\) needs MCP_TRANSPORT=stdio"):
            await serve_stdio(self._settings("http"))

    def test_build_app_still_accepts_its_own(self) -> None:
        app = build_app(self._settings("http"))
        assert {getattr(r, "path", None) for r in app.routes} >= {"/health", "/wger-mcp"}


class TestExplicitEnvFile:
    """--env-file is a promise to read *that* file, so a missing one is fatal."""

    def test_a_missing_path_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="env file not found"):
            env_file_for(Transport.stdio, str(tmp_path / "typo.env"))

    def test_an_existing_path_is_returned(self, tmp_path: Path) -> None:
        path = tmp_path / "wger.env"
        path.write_text("WGER_API_KEY=k\n")
        assert env_file_for(Transport.stdio, str(path)) == str(path)

    def test_a_directory_is_not_a_settings_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            env_file_for(Transport.http, str(tmp_path))

    def test_the_default_stays_optional(self, tmp_path: Path, monkeypatch) -> None:
        """Only the explicit flag is strict — ./.env may legitimately be absent."""
        monkeypatch.chdir(tmp_path)
        assert env_file_for(Transport.http) == DEFAULT_ENV_FILE

    def test_the_typo_stops_the_process_with_a_sentence(self, tmp_path: Path) -> None:
        """Before: it started against whatever the environment still held."""
        proc = subprocess.run(
            [sys.executable, "-m", "wger_mcp.server", "--transport", "stdio",
             "--env-file", str(tmp_path / "typo.env")],
            input="",
            capture_output=True,
            text=True,
            env=_env(WGER_BASE_URL="https://leftover.test"),
            timeout=_TIMEOUT,
        )

        assert proc.returncode != 0
        assert "env file not found" in proc.stderr
        assert "Traceback" not in proc.stderr
        assert "leftover.test" not in proc.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_sigint_shuts_down_quietly() -> None:
    """A stop is not a crash, and the client's log pane shows every line of it.

    The handshake is what makes this deterministic: once a response has come
    back, the server is demonstrably serving, so the signal lands on a running
    process rather than on whatever a sleep happened to catch.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "wger_mcp.server", "--transport", "stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(),
    )
    assert proc.stdin and proc.stdout
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "sigint-test", "version": "1"},
        },
    }
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    assert proc.stdout.readline().strip(), "server never answered initialize"

    proc.send_signal(signal.SIGINT)
    _, err = proc.communicate(timeout=_TIMEOUT)

    assert proc.returncode == 0, f"expected a clean exit, got {proc.returncode}: {err}"
    assert "Traceback" not in err, err
    assert "shutting down" in err


def test_the_http_caller_passes_its_own_fastmcp_options() -> None:
    """All three are the caller's to hand over now, so pin them where they show.

    Dropping one is invisible in a unit assertion but obvious here: the mount
    path 404s, a configured host 421s, and the body arrives as SSE.
    """
    settings = Settings(  # type: ignore[call-arg]
        wger_base_url="https://wger.test",
        mcp_transport="http",
        mcp_auth="none",
        wger_dev_token="k",
        allowed_hosts=["mcp.example.com"],
        mcp_path="/wger-mcp",
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "opts-test", "version": "1"},
        },
    }
    headers = {"Accept": "application/json, text/event-stream"}

    with TestClient(build_app(settings), base_url="http://mcp.example.com") as client:
        response = client.post("/wger-mcp", json=request, headers=headers)

    assert response.status_code == 200, response.text  # streamable_http_path
    assert response.headers["content-type"].startswith("application/json")  # json_response
    assert response.json()["result"]["serverInfo"]["name"] == "wger"  # transport_security


def test_the_docker_image_pins_its_transport() -> None:
    """The image is an HTTP server: it exposes a port and health-checks a listener.

    Left to MCP_TRANSPORT, a stdio container finds stdin at /dev/null, exits 0 at
    once, and `restart: unless-stopped` respawns it forever — a crash loop that
    every exit code reports as success. Cheap to assert, expensive to debug.
    """
    dockerfile = Path(__file__).resolve().parent.parent / "Dockerfile"
    cmd = [ln for ln in dockerfile.read_text().splitlines() if ln.startswith("CMD ")]

    assert cmd, "no CMD in the Dockerfile"
    assert '"--transport", "http"' in cmd[-1], cmd[-1]


def test_the_flag_overriding_the_environment_is_announced() -> None:
    """Pinning the transport must not make MCP_TRANSPORT silently do nothing."""
    proc = subprocess.run(
        [sys.executable, "-m", "wger_mcp.server", "--transport", "stdio"],
        input="",
        capture_output=True,
        text=True,
        env=_env(MCP_TRANSPORT="http"),
        timeout=_TIMEOUT,
    )

    assert "--transport stdio overrides MCP_TRANSPORT=http" in proc.stderr
    assert proc.returncode == 0


def test_an_unset_variable_is_not_announced_as_overridden() -> None:
    """The default is not a value someone chose, and must not be reported as one.

    Every client start passes --transport stdio into an environment that says
    nothing; a warning here would be both wrong and permanent log noise.
    """
    env = {k: v for k, v in _env().items() if k.upper() != "MCP_TRANSPORT"}
    proc = subprocess.run(
        [sys.executable, "-m", "wger_mcp.server", "--transport", "stdio"],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=_TIMEOUT,
    )

    assert proc.returncode == 0
    assert "overrides MCP_TRANSPORT" not in proc.stderr, proc.stderr


class TestStdioAuthStrategy:
    """Under stdio the strategy is not a choice — so it may not look like one."""

    @staticmethod
    def _load(**env: str) -> Settings:
        os.environ.update({"WGER_BASE_URL": "https://wger.test", "WGER_API_KEY": "k", **env})
        return load_settings(env_file=None, mcp_transport="stdio")

    @pytest.mark.parametrize("strategy", ["oidc", "static_token"])
    def test_a_chosen_strategy_is_refused_rather_than_rewritten(self, strategy: str) -> None:
        with pytest.raises(ValidationError, match=f"cannot use MCP_AUTH={strategy}"):
            self._load(MCP_AUTH=strategy, MCP_STATIC_TOKEN="short")

    def test_saying_none_out_loud_is_allowed(self) -> None:
        assert self._load(MCP_AUTH="none").mcp_auth is AuthStrategy.none

    def test_the_default_is_filled_in_when_nobody_chose(self) -> None:
        """Rewriting an unset field is picking a default, not discarding config."""
        assert self._load().mcp_auth is AuthStrategy.none
