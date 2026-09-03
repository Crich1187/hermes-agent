"""Tests for the per-server ``oauth.user_agent`` on MCP OAuth token requests.

Some authorization servers and WAFs reject httpx's default User-Agent on the
token endpoint (#75576). The header is opt-in, per-server, and applied ONLY to
the two token-endpoint requests (authorization-code exchange and refresh) —
never to MCP traffic or discovery.

The tests drive the REAL provider classes' request builders end to end: the
``httpx.Request`` the SDK would send is what gets inspected, not a mocked
constructor call.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "mcp.client.auth.oauth2",
    reason="MCP SDK required for OAuth support",
)

from tools.mcp_oauth import (  # noqa: E402 — after the SDK availability gate
    build_oauth_auth,
    authorization_response_issuer_aliases,
    normalize_authorization_response_issuer,
    token_request_headers_from_env,
    token_request_user_agent,
)


def _set_interactive_stdin(monkeypatch, *, is_tty: bool = True) -> None:
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = is_tty
    monkeypatch.setattr("tools.mcp_oauth.sys.stdin", mock_stdin)


@pytest.fixture(autouse=True)
def clean_port_state():
    import tools.mcp_oauth as mod

    mod._assigned_cimd_ports.clear()
    yield
    mod._assigned_cimd_ports.clear()
    for port in list(mod._reserved_sockets):
        sock = mod._reserved_sockets.pop(port, None)
        if sock is not None:
            sock.close()


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_configured_user_agent_is_returned():
    assert token_request_user_agent({"user_agent": "My-MCP-Client/1.0"}) == "My-MCP-Client/1.0"


@pytest.mark.parametrize("cfg", [
    pytest.param({}, id="absent"),
    pytest.param({"user_agent": None}, id="null"),
    pytest.param({"user_agent": ""}, id="empty"),
    pytest.param({"user_agent": "   "}, id="whitespace-only"),
    pytest.param({"user_agent": 7}, id="non-string"),
])
def test_unset_user_agent_values_are_treated_as_absent(cfg):
    assert token_request_user_agent(cfg) is None


def test_user_agent_is_stripped():
    assert token_request_user_agent({"user_agent": "  UA/2 "}) == "UA/2"


def test_token_endpoint_headers_resolve_only_from_environment(monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_KEY", "secret-value")
    assert token_request_headers_from_env(
        {"token_endpoint_headers_env": {"x-gateway-key": "MCP_GATEWAY_KEY"}}
    ) == {"x-gateway-key": "secret-value"}


@pytest.mark.parametrize(
    "cfg",
    [
        pytest.param({"token_endpoint_headers_env": []}, id="not-a-mapping"),
        pytest.param(
            {"token_endpoint_headers_env": {"bad header": "MCP_GATEWAY_KEY"}},
            id="bad-header",
        ),
        pytest.param(
            {"token_endpoint_headers_env": {"x-gateway-key": "bad-env-name"}},
            id="bad-env",
        ),
    ],
)
def test_invalid_token_endpoint_header_config_is_rejected(cfg, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_KEY", "secret-value")
    with pytest.raises(ValueError):
        token_request_headers_from_env(cfg)


def test_missing_token_endpoint_header_environment_is_rejected(monkeypatch):
    monkeypatch.delenv("MCP_GATEWAY_KEY", raising=False)
    with pytest.raises(ValueError, match="MCP_GATEWAY_KEY"):
        token_request_headers_from_env(
            {"token_endpoint_headers_env": {"x-gateway-key": "MCP_GATEWAY_KEY"}}
        )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("value\twith-tab", id="tab"),
        pytest.param("value\r\nwith-newline", id="newline"),
        pytest.param("caf\N{LATIN SMALL LETTER E WITH ACUTE}", id="non-ascii"),
    ],
)
def test_unsafe_token_endpoint_header_values_are_rejected(value, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_KEY", value)
    with pytest.raises(ValueError):
        token_request_headers_from_env(
            {"token_endpoint_headers_env": {"x-gateway-key": "MCP_GATEWAY_KEY"}}
        )


@pytest.mark.parametrize(
    "header_name",
    ["Authorization", "Host", "Content-Length", "Transfer-Encoding", "User-Agent"],
)
def test_reserved_token_endpoint_headers_are_rejected(header_name, monkeypatch):
    monkeypatch.setenv("MCP_GATEWAY_KEY", "secret-value")
    with pytest.raises(ValueError, match="reserved header"):
        token_request_headers_from_env(
            {"token_endpoint_headers_env": {header_name: "MCP_GATEWAY_KEY"}}
        )


def test_authorization_response_issuer_aliases_are_exact_https_mappings():
    assert authorization_response_issuer_aliases(
        {
            "authorization_response_issuer_aliases": {
                "https://auth.example.com/oauth": "https://gateway.example.com/mcp"
            }
        }
    ) == {"https://auth.example.com/oauth": "https://gateway.example.com/mcp"}


@pytest.mark.parametrize(
    "aliases",
    [
        pytest.param([], id="not-a-mapping"),
        pytest.param(
            {"http://auth.example.com": "https://gateway.example.com/mcp"},
            id="upstream-not-https",
        ),
        pytest.param(
            {"https://auth.example.com": "https://user@gateway.example.com/mcp"},
            id="gateway-userinfo",
        ),
        pytest.param(
            {"https://auth.example.com": "https://gateway.example.com/mcp?bad=1"},
            id="gateway-query",
        ),
    ],
)
def test_invalid_authorization_response_issuer_aliases_are_rejected(aliases):
    with pytest.raises(ValueError):
        authorization_response_issuer_aliases(
            {"authorization_response_issuer_aliases": aliases}
        )


def test_authorization_response_issuer_alias_is_exact():
    aliases = {
        "https://auth.example.com/oauth": "https://gateway.example.com/mcp"
    }
    assert normalize_authorization_response_issuer(
        "https://auth.example.com/oauth", aliases
    ) == "https://gateway.example.com/mcp"
    assert normalize_authorization_response_issuer(
        "https://unexpected.example.com", aliases
    ) == "https://unexpected.example.com"
    assert normalize_authorization_response_issuer(None, aliases) is None


# ---------------------------------------------------------------------------
# The requests the SDK actually sends
# ---------------------------------------------------------------------------


def _ready_for_token_requests(provider):
    """Give the provider the minimum context both builders require."""
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

    provider.context.oauth_metadata = SimpleNamespace(
        token_endpoint="https://idp.example.com/oauth/token"
    )
    provider.context.client_info = OAuthClientInformationFull.model_validate({
        "client_id": "client-1",
        "redirect_uris": ["http://127.0.0.1:33333/callback"],
    })
    provider.context.current_tokens = OAuthToken.model_validate({
        "access_token": "at",
        "token_type": "Bearer",
        "refresh_token": "rt",
    })


def _build_provider_via(builder, monkeypatch, tmp_path, cfg):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _set_interactive_stdin(monkeypatch)
    return builder("srv", "https://mcp.example.com/mcp", cfg)


def _manager_builder(server_name, server_url, cfg):
    from tools.mcp_oauth_manager import MCPOAuthManager, reset_manager_for_tests

    reset_manager_for_tests()
    return MCPOAuthManager().get_or_build_provider(server_name, server_url, cfg)


@pytest.mark.parametrize("builder", [
    pytest.param(build_oauth_auth, id="build_oauth_auth"),
    pytest.param(_manager_builder, id="oauth_manager"),
])
def test_token_requests_carry_the_configured_user_agent(
    builder, tmp_path, monkeypatch
):
    """Both token-endpoint requests, on both provider construction paths."""
    provider = _build_provider_via(
        builder, monkeypatch, tmp_path, {"user_agent": "My-MCP-Client/1.0"}
    )
    _ready_for_token_requests(provider)

    exchange = asyncio.run(
        provider._exchange_token_authorization_code("code", "verifier")
    )
    refresh = asyncio.run(provider._refresh_token())

    assert exchange.headers["User-Agent"] == "My-MCP-Client/1.0"
    assert refresh.headers["User-Agent"] == "My-MCP-Client/1.0"


@pytest.mark.parametrize("builder", [
    pytest.param(build_oauth_auth, id="build_oauth_auth"),
    pytest.param(_manager_builder, id="oauth_manager"),
])
def test_unconfigured_user_agent_leaves_the_default_header(
    builder, tmp_path, monkeypatch
):
    """No config → httpx's own default, exactly as before the feature."""
    import httpx

    provider = _build_provider_via(builder, monkeypatch, tmp_path, {})
    _ready_for_token_requests(provider)

    exchange = asyncio.run(
        provider._exchange_token_authorization_code("code", "verifier")
    )
    refresh = asyncio.run(provider._refresh_token())

    default_ua = httpx.Request("POST", "https://x.example/").headers.get("User-Agent")
    assert exchange.headers.get("User-Agent") == default_ua
    assert refresh.headers.get("User-Agent") == default_ua


@pytest.mark.parametrize("builder", [
    pytest.param(build_oauth_auth, id="build_oauth_auth"),
    pytest.param(_manager_builder, id="oauth_manager"),
])
def test_token_requests_carry_headers_resolved_from_environment(
    builder, tmp_path, monkeypatch
):
    monkeypatch.setenv("MCP_GATEWAY_KEY", "secret-value")
    provider = _build_provider_via(
        builder,
        monkeypatch,
        tmp_path,
        {"token_endpoint_headers_env": {"x-gateway-key": "MCP_GATEWAY_KEY"}},
    )
    _ready_for_token_requests(provider)

    exchange = asyncio.run(
        provider._exchange_token_authorization_code("code", "verifier")
    )
    refresh = asyncio.run(provider._refresh_token())

    assert exchange.headers["x-gateway-key"] == "secret-value"
    assert refresh.headers["x-gateway-key"] == "secret-value"

    resource = __import__("httpx").Request(
        "POST", "https://mcp.example.com/mcp"
    )
    provider._add_auth_header(resource)
    assert "x-gateway-key" not in resource.headers


def test_user_agent_does_not_disturb_token_auth_preparation(tmp_path, monkeypatch):
    """The stamp runs after prepare_token_auth — a confidential client's
    Authorization header must survive alongside the custom User-Agent."""
    provider = _build_provider_via(
        build_oauth_auth, monkeypatch, tmp_path,
        {"user_agent": "UA/1", "client_id": "pre", "client_secret": "shh",
         "token_endpoint_auth_method": "client_secret_basic"},
    )
    _ready_for_token_requests(provider)
    from mcp.shared.auth import OAuthClientInformationFull

    provider.context.client_info = OAuthClientInformationFull.model_validate({
        "client_id": "pre",
        "client_secret": "shh",
        "token_endpoint_auth_method": "client_secret_basic",
        "redirect_uris": ["http://127.0.0.1:33333/callback"],
    })

    exchange = asyncio.run(
        provider._exchange_token_authorization_code("code", "verifier")
    )

    assert exchange.headers["User-Agent"] == "UA/1"
    assert exchange.headers.get("Authorization", "").startswith("Basic ")
