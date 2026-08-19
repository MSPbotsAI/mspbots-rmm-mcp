"""Gateway credential middleware tests: missing-header 401, and header
values correctly reaching the per-request contextvar (no global-state
leakage across requests).
"""

from starlette.testclient import TestClient

from mspbots_fleet_mcp.__main__ import _build_http_app
from mspbots_fleet_mcp.config import Settings
from mspbots_fleet_mcp.server import create_mcp_server, get_client_from_context


def _make_app():
    settings = Settings()
    mcp = create_mcp_server(settings)
    return _build_http_app(mcp, settings), settings


def test_health_is_local_and_does_not_require_credentials():
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_missing_header_returns_401_with_required_headers_listed():
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert resp.status_code == 401
        body = resp.json()
        assert body["required_headers"] == ["X-MSP-Token", "X-MSP-Tenant-Id", "X-MSP-Host"]


def test_missing_single_header_still_returns_401():
    app, _ = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={
                "Accept": "application/json, text/event-stream",
                "X-MSP-Token": "dummy-token",
                "X-MSP-Tenant-Id": "dummy-tenant",
                # X-MSP-Host intentionally omitted
            },
        )
        assert resp.status_code == 401


def test_header_present_reaches_request_context(monkeypatch):
    # Directly exercises the middleware's contextvar plumbing without a full
    # MCP protocol round-trip: confirms the header values that arrive on the
    # request are exactly what get_client_from_context sees, and that they
    # are reset afterward (no leakage to the next request).
    import asyncio

    from mspbots_fleet_mcp.server import GatewayTokenMiddleware, _gateway_creds_var

    settings = Settings()
    seen = {}

    async def fake_app(scope, receive, send):
        seen["creds"] = _gateway_creds_var.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = GatewayTokenMiddleware(fake_app, settings)

    async def run():
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [
                (b"x-msp-token", b"test-token-123"),
                (b"x-msp-tenant-id", b"tenant-abc"),
                (b"x-msp-host", b"https://agent.mspbots.ai"),
            ],
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        await middleware(scope, receive, send)

    asyncio.run(run())
    assert seen["creds"] == ("test-token-123", "https://agent.mspbots.ai", "tenant-abc")
    # After the request completes, the contextvar must be reset — a fresh
    # get() outside any request context sees no leftover credential.
    assert _gateway_creds_var.get() is None


def test_client_factory_returns_none_without_context():
    settings = Settings()
    assert get_client_from_context(settings) is None


def test_parse_claim_hosts_header_splits_on_comma_semicolon_whitespace():
    from mspbots_fleet_mcp.server import _parse_claim_hosts_header

    assert _parse_claim_hosts_header("mac-01, C02XY1234; \t uuid-1  ,,;") == [
        "mac-01",
        "C02XY1234",
        "uuid-1",
    ]
    assert _parse_claim_hosts_header("") == []
    assert _parse_claim_hosts_header("   ") == []


def test_rewrite_sse_instructions_appends_note():
    from mspbots_fleet_mcp.server import _rewrite_sse_instructions

    raw = (
        b'event: message\n'
        b'data: {"jsonrpc":"2.0","id":1,"result":{"instructions":"base text"}}\n\n'
    )
    rewritten = _rewrite_sse_instructions(raw, "extra note")
    text = rewritten.decode("utf-8")
    assert text.startswith("event: message\n")
    assert '"instructions":"base text\\n\\nextra note"' in text


def test_rewrite_sse_instructions_leaves_non_matching_body_untouched():
    from mspbots_fleet_mcp.server import _rewrite_sse_instructions

    raw = b'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n\n'
    assert _rewrite_sse_instructions(raw, "extra note") == raw


def test_claim_on_connect_appends_note_to_initialize_response(monkeypatch):
    # Full middleware exercise with a stubbed FleetClient (no real network
    # call) and a fake downstream app returning a canned SSE initialize
    # response, confirming the note lands in the response body the caller
    # actually receives.
    import asyncio
    import json

    from mspbots_fleet_mcp import server as server_module
    from mspbots_fleet_mcp.server import GatewayTokenMiddleware

    class FakeFleetClient:
        def __init__(self, token, host, tenant_id):
            pass

        async def post(self, path, body):
            assert path == "/api/fleet/hosts/claim"
            assert body == {"identifiers": ["mac-01", "C02XY1234"]}
            return {"claimed": [{"id": 1}], "waiting": ["c02xy1234"], "rejected": []}

    monkeypatch.setattr(server_module, "FleetClient", FakeFleetClient)

    settings = Settings()

    async def fake_app(scope, receive, send):
        # Drain the (replayed) request body like a real app would.
        await receive()
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"instructions": "base"}}
        ).encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send(
            {
                "type": "http.response.body",
                "body": b"event: message\ndata: " + body + b"\n\n",
                "more_body": False,
            }
        )

    middleware = GatewayTokenMiddleware(fake_app, settings)

    async def run():
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [
                (b"x-msp-token", b"tok"),
                (b"x-msp-tenant-id", b"tenant"),
                (b"x-msp-host", b"https://agent.mspbots.ai"),
                (b"x-claim-hosts", b"mac-01, C02XY1234"),
            ],
        }
        request_body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}).encode()

        async def receive():
            return {"type": "http.request", "body": request_body, "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        await middleware(scope, receive, send)
        return sent

    sent = asyncio.run(run())
    body_message = next(m for m in sent if m["type"] == "http.response.body")
    text = body_message["body"].decode()
    assert "base\\n\\nThis connector declared 2 machine(s) on connect" in text
    assert "1 claimed, 1 waiting to enrol, 0 owned by another tenant" in text


def test_claim_on_connect_ignored_for_non_initialize_request(monkeypatch):
    import asyncio
    import json

    from mspbots_fleet_mcp import server as server_module
    from mspbots_fleet_mcp.server import GatewayTokenMiddleware

    class ExplodingFleetClient:
        def __init__(self, *a, **k):
            pass

        async def post(self, *a, **k):
            raise AssertionError("claim_hosts must not be called for a non-initialize request")

    monkeypatch.setattr(server_module, "FleetClient", ExplodingFleetClient)

    settings = Settings()
    original_body = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()

    async def fake_app(scope, receive, send):
        message = await receive()
        assert message["body"] == original_body
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    middleware = GatewayTokenMiddleware(fake_app, settings)

    async def run():
        scope = {
            "type": "http",
            "path": "/mcp",
            "headers": [
                (b"x-msp-token", b"tok"),
                (b"x-msp-tenant-id", b"tenant"),
                (b"x-msp-host", b"https://agent.mspbots.ai"),
                (b"x-claim-hosts", b"mac-01"),
            ],
        }

        async def receive():
            return {"type": "http.request", "body": original_body, "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        await middleware(scope, receive, send)
        return sent

    sent = asyncio.run(run())
    body_message = next(m for m in sent if m["type"] == "http.response.body")
    assert body_message["body"] == b"ok"
