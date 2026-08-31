"""Device inventory tools.

Devices are read-only here: a machine joins by installing the connected
RMM's agent with the tenant's enrollment secret, and leaves by being
removed upstream — there is no create or delete endpoint on this API.

`reboot_device` (POST /devices/{id}/reboot) is deliberately NOT registered
as a tool: per the RMM Control API docs, the route exists but no vendor
adapter implements it yet, so every call answers 501 — Fleet has no reboot
API at all. Add it once a real adapter implements the capability.
"""

from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped
from ..api_client import RmmClient, RmmError
from ._common import CONNECTION_DESC, NO_TOKEN

_MAX_PAGE_SIZE = 200


def register(mcp: FastMCP, client_factory: Callable[[], RmmClient | None]) -> None:

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsrmm_list_devices(
        connection: Annotated[str | None, Field(description=CONNECTION_DESC)] = None,
        search: Annotated[
            str | None,
            Field(
                description="Free-text match on hostname, display name, serial and IP. "
                "Substring, case-insensitive. Prefer this over paging the whole fleet "
                "when looking for one known machine."
            ),
        ] = None,
        status: Annotated[
            str | None, Field(description='Reachability filter: "online" | "offline" | "missing".')
        ] = None,
        platform: Annotated[
            str | None,
            Field(
                description='OS family as the RMM reports it, e.g. "darwin", '
                '"windows", "ubuntu", "rhel".'
            ),
        ] = None,
        page: Annotated[int, Field(description="Zero-based page index.")] = 0,
        per_page: Annotated[int, Field(description="Items per page (max 200).")] = 25,
    ) -> str:
        """List the machines this tenant manages, newest inventory first.

        Use this to answer "which machines do we have", to find a device id
        before running a script, or to check whether a specific host is
        currently reachable. Results come live from the connected RMM, not a
        local cache, so status and last-seen time reflect the moment of the
        call. Filters combine with AND.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        per_page = min(per_page, _MAX_PAGE_SIZE)
        try:
            result = await client.get(
                "/api/rmm/devices",
                params={
                    "connection": connection,
                    "search": search,
                    "status": status,
                    "platform": platform,
                    "page": page,
                    "per_page": per_page,
                },
            )
            return dump_json_capped(result)
        except RmmError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsrmm_get_device(
        id: Annotated[str, Field(description="Device id exactly as returned by list_devices.")],
        connection: Annotated[str | None, Field(description=CONNECTION_DESC)] = None,
    ) -> str:
        """Read one device in full: everything list_devices returns plus
        hardware identifiers (serial/UUID/MAC/hostname), vendor-specific
        attributes, and the capability list for its connection.

        Use this before acting on a device — to confirm it's the right
        machine, to read its serial or MAC, or to check whether an action is
        supported (via the returned capabilities list) before attempting it.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.get(f"/api/rmm/devices/{id}", params={"connection": connection})
            return dump_json_capped(result)
        except RmmError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def mspbotsrmm_refetch_device(
        id: Annotated[str, Field(description="Device id exactly as returned by list_devices.")],
        connection: Annotated[str | None, Field(description=CONNECTION_DESC)] = None,
    ) -> str:
        """Ask the agent to re-report its state on its next check-in, instead
        of waiting for the normal inventory cycle.

        Use when a device's details look stale after a change was made on
        the machine. Does not return fresh data — it schedules the
        collection; call mspbotsrmm_get_device afterwards to see it. Safe to
        call repeatedly. Not every connected RMM supports this — an
        unsupported connection returns a permanent error naming the vendor.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            await client.post(f"/api/rmm/devices/{id}/refetch", {"connection": connection})
        except RmmError as e:
            return e.to_envelope()
        return dump_json_capped({"id": id, "refetch": "queued"})
