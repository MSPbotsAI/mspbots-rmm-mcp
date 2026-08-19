import contextvars
import json
import re
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api_client import FleetClient, FleetError
from .config import Settings

_IDENTIFIER_SPLIT_RE = re.compile(r"[,;\s]+")


def _parse_claim_hosts_header(value: str) -> list[str]:
    return [part for part in _IDENTIFIER_SPLIT_RE.split(value) if part]


async def _buffer_request_body(receive: Receive) -> tuple[bytes, Receive]:
    """Read the full request body and return it alongside a replacement
    receive() that replays the same messages, so the wrapped ASGI app can
    still consume the body normally."""
    body = b""
    more_body = True
    while more_body:
        message = await receive()
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        more_body = message.get("more_body", False)

    replayed: Message | None = {"type": "http.request", "body": body, "more_body": False}

    async def replay_receive() -> Message:
        nonlocal replayed
        if replayed is not None:
            message, replayed = replayed, None
            return message
        return await receive()

    return body, replay_receive


def _is_initialize_request(body: bytes) -> bool:
    try:
        payload = json.loads(body)
    except ValueError:
        return False
    return isinstance(payload, dict) and payload.get("method") == "initialize"


def _format_claim_summary(declared_count: int, outcome: dict) -> str:
    claimed = len(outcome.get("claimed") or [])
    waiting = len(outcome.get("waiting") or [])
    rejected = len(outcome.get("rejected") or [])
    return (
        f"This connector declared {declared_count} machine(s) on connect: "
        f"{claimed} claimed, {waiting} waiting to enrol, {rejected} owned by "
        "another tenant."
    )


async def _run_claim_on_connect(client: FleetClient, identifiers: list[str]) -> str:
    try:
        outcome = await client.post("/api/fleet/hosts/claim", {"identifiers": identifiers})
    except FleetError as e:
        return f"Host claim on connect failed: {e.message}"
    return _format_claim_summary(len(identifiers), outcome or {})


def _rewrite_sse_instructions(raw: bytes, note: str) -> bytes:
    """Append `note` to the initialize response's result.instructions field.

    The streamable-http transport wraps the JSON-RPC response in an SSE
    frame ("event: message\\ndata: {...}\\n\\n"); this rewrites only the
    `data:` line's JSON payload and leaves the rest of the frame untouched.
    """
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[len("data:") :].strip())
        except ValueError:
            continue
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict) or "instructions" not in result:
            continue
        existing = result.get("instructions") or ""
        result["instructions"] = f"{existing}\n\n{note}" if existing else note
        lines[i] = "data: " + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return "\n".join(lines).encode("utf-8")
    return raw


def _wrap_send_to_inject_note(send: Send, note: str) -> Send:
    """Buffer the (small, single-message) initialize response body so it
    can be rewritten in one piece before being sent."""
    body_chunks: list[bytes] = []

    async def wrapped_send(message: Message) -> None:
        if message["type"] == "http.response.body":
            body_chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            modified = _rewrite_sse_instructions(b"".join(body_chunks), note)
            await send({"type": "http.response.body", "body": modified, "more_body": False})
            return
        await send(message)

    return wrapped_send

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

    Also implements the optional X-Claim-Hosts header: on an `initialize`
    request only, it runs the equivalent of a claim_hosts call and appends
    the outcome to that response's `instructions` field. Every other
    request ignores the header, matching the upstream API contract.
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
                    "optional_headers": ["X-Claim-Hosts"],
                },
                status_code=401,
            )
            await response(scope, receive, send)
            return

        claim_hosts_header = request.headers.get("x-claim-hosts")
        identifiers = _parse_claim_hosts_header(claim_hosts_header) if claim_hosts_header else []
        if identifiers:
            body, receive = await _buffer_request_body(receive)
            if _is_initialize_request(body):
                note = await _run_claim_on_connect(FleetClient(token, host, tenant_id), identifiers)
                send = _wrap_send_to_inject_note(send, note)

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
            "Every resource is scoped to one tenant only. Tool groups: "
            "mspbotsfleet_*_host* manage host inventory, labels, and "
            "claim/release; mspbotsfleet_*_script* manage and run scripts; "
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
