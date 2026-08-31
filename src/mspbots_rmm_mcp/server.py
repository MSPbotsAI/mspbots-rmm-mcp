import contextvars
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .api_client import RmmClient
from .config import Settings

# Per-request credential isolation via contextvars.
# GatewayTokenMiddleware sets this before the MCP handler runs.
# Python asyncio copies context per task, so concurrent SSE connections are isolated.
# Value is (access_token, host, tenant_id). tenant_id is forwarded to the
# downstream RMM Control API as an X_Tenant_ID header.
_gateway_creds_var: contextvars.ContextVar[tuple[str, str, str] | None] = contextvars.ContextVar(
    "mspbots_rmm_gateway_creds", default=None
)


def get_client_from_context(settings: Settings) -> RmmClient | None:
    """Resolve the active RmmClient for the current request context."""
    creds = _gateway_creds_var.get()
    if not creds:
        return None
    token, host, tenant_id = creds
    return RmmClient(token, host, tenant_id)


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
                        "This server requires the X-MSP-Token header (RMM Control "
                        "API bearer access credential), the X-MSP-Tenant-Id header, "
                        "and the X-MSP-Host header (RMM Control API host)"
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
    """Build the FastMCP server instance and register all RMM Control tools."""
    # DNS-rebinding protection is a browser-oriented safeguard that rejects
    # non-localhost Host headers with 421. Disable it so the server works
    # correctly behind a reverse proxy or docker network.
    mcp = FastMCP(
        name="mspbots-rmm-mcp",
        instructions=(
            "MSPbots RMM Control (mb-platform-rmm) is a vendor-agnostic remote "
            "monitoring and management console — this server exposes that "
            "platform's own REST API (an internal MSPbots product, not a "
            "third-party integration) as MCP tools. The same endpoints work "
            "whichever RMM vendor a tenant has connected (Fleet today, others "
            "later), proxied through a vendor adapter. Core concepts: a device "
            "is a machine enrolled via the connected RMM's agent — read-only "
            "here, it joins/leaves upstream, not through this API; a script "
            "(.sh/.ps1/.py, depending on vendor) can be run on one or more "
            "devices on demand; every call is scoped to one tenant. Not every "
            "connected vendor supports every action — an unsupported call "
            "returns a permanent, non-retryable error naming the vendor, "
            "rather than failing silently. Tool groups: mspbotsrmm_*_device* "
            "read device inventory and ask a device to refresh; "
            "mspbotsrmm_*_script* manage the script library; "
            "mspbotsrmm_run_script/mspbotsrmm_list_script_runs dispatch "
            "execution and read results back (asynchronous — dispatch "
            "returns immediately, poll list_script_runs for output). Typical "
            "flow: list devices to find a target, then run a script against "
            "it and poll for the result. Credentials come only from request "
            "headers, never tool arguments."
        ),
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    client_factory: Callable[[], RmmClient | None] = lambda: get_client_from_context(settings)

    from .tools import devices, execution, scripts

    devices.register(mcp, client_factory)
    scripts.register(mcp, client_factory)
    execution.register(mcp, client_factory)

    return mcp
