import contextvars
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import FleetClient
from .config import Settings

# Per-request credential isolation via contextvars.
# GatewayTokenMiddleware sets this before the MCP handler runs.
# Python asyncio copies context per task, so concurrent SSE connections are isolated.
# Value is (access_token, host, tenant_id). tenant_id is forwarded to the
# downstream Fleet API as an X_Tenant_ID header.
_gateway_creds_var: contextvars.ContextVar[tuple[str, str, str] | None] = contextvars.ContextVar(
    "mspbots_fleet_gateway_creds", default=None
)


def get_client_from_context(settings: Settings) -> FleetClient | None:
    """Resolve the active FleetClient for the current request context."""
    creds = _gateway_creds_var.get()
    if not creds:
        return None
    token, host, tenant_id = creds
    return FleetClient(token, host, tenant_id)


class GatewayTokenMiddleware:
    """ASGI middleware.

    Reads X-MSP-Token, X-MSP-Tenant-Id, and X-MSP-Host (all required) from
    request headers and stores them in the contextvar. Returns 401 if any is
    missing on /mcp requests.
    """

    def __init__(self, app: ASGIApp, settings: Settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        token = request.headers.get("x-msp-token")
        tenant_id = request.headers.get("x-msp-tenant-id")
        host = request.headers.get("x-msp-host")
        if not token or not tenant_id or not host:
            response = JSONResponse(
                {
                    "error": "Missing credentials",
                    "message": (
                        "This server requires the X-MSP-Token header (Fleet Platform "
                        "bearer access credential), the X-MSP-Tenant-Id header, and "
                        "the X-MSP-Host header (Fleet API host)"
                    ),
                    "required_headers": ["X-MSP-Token", "X-MSP-Tenant-Id", "X-MSP-Host"],
                    "optional_headers": [],
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        ctx_token = _gateway_creds_var.set((token, host, tenant_id))
        try:
            await self.app(scope, receive, send)
        finally:
            _gateway_creds_var.reset(ctx_token)


def create_mcp_server(settings: Settings) -> FastMCP:
    """Build the FastMCP server instance and register all Fleet tools."""
    # DNS-rebinding protection is a browser-oriented safeguard that rejects
    # non-localhost Host headers with 421. Disable it so the server works
    # correctly behind a reverse proxy or docker network.
    mcp = FastMCP(
        name="mspbots-fleet-mcp",
        instructions=(
            "MSPbots Fleet Platform is a multi-tenant console over fleetdm — this "
            "server exposes that platform's own REST API (an internal MSPbots "
            "product, not a third-party integration) as MCP tools. Core concepts: "
            "a host is a machine running fleetd, scoped to a tenant once claimed; "
            "a script (.sh/.ps1) can be run on a host on demand; a saved query is "
            "an osquery SELECT that can be run live against one or more hosts. "
            "Scripts and queries can be tenant-private or shared org-wide. Tool "
            "groups: mspbotsfleet_*_host* manage host inventory, labels, and "
            "adoption; mspbotsfleet_*_script* manage and run scripts; "
            "mspbotsfleet_*_query* manage and run saved osquery SELECTs. Typical "
            "flow: list hosts to find a target, then run a script or query "
            "against it and poll for the result. Credentials come only from "
            "request headers, never tool arguments."
        ),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    client_factory: Callable[[], FleetClient | None] = lambda: get_client_from_context(settings)

    from .tools import hosts, queries, scripts

    hosts.register(mcp, client_factory)
    scripts.register(mcp, client_factory)
    queries.register(mcp, client_factory)

    return mcp
