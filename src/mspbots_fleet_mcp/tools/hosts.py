from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped
from ..api_client import FleetClient, FleetError
from ._common import NO_TOKEN

_MAX_PAGE_SIZE = 200


def register(mcp: FastMCP, client_factory: Callable[[], FleetClient | None]) -> None:

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsfleet_list_hosts(
        search: Annotated[
            str | None,
            Field(description="Fuzzy match against hostname/computer name/display name/IP."),
        ] = None,
        status: Annotated[
            str | None, Field(description='"online" | "offline" | "new" | "missing".')
        ] = None,
        page: Annotated[int, Field(description="0-based page number.")] = 0,
        per_page: Annotated[int, Field(description="Rows per page (max 200).")] = 25,
    ) -> str:
        """List the current tenant's hosts, optionally filtered by search/status.

        Each row carries id, hostname, platform, status, primary_ip, and
        seen_time. The full inventory is fetched and cached upstream for
        15s, so results can lag a claim/release/delete briefly.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        per_page = min(per_page, _MAX_PAGE_SIZE)
        try:
            result = await client.get(
                "/api/fleet/hosts",
                params={"search": search, "status": status, "page": page, "per_page": per_page},
            )
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsfleet_get_host(
        host_id: Annotated[int, Field(description="Fleet host id.")],
    ) -> str:
        """Get a host's full details plus its manual (non-dynamic) labels."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.get(f"/api/fleet/hosts/{host_id}")
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsfleet_list_host_scripts(
        host_id: Annotated[int, Field(description="Fleet host id.")],
    ) -> str:
        """List the scripts a host can run, already filtered to what this
        tenant can see, each with its last execution (if any)."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.get(f"/api/fleet/hosts/{host_id}/scripts")
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def mspbotsfleet_set_host_labels(
        host_id: Annotated[int, Field(description="Fleet host id.")],
        labels: Annotated[
            list[str],
            Field(
                description=(
                    "The COMPLETE set of manual label names this host should end up "
                    "with (each must already exist as a manual label in Fleet) — "
                    "not a delta. Any manual label not in this list gets removed. "
                    "To add or remove just one label without disturbing the rest, "
                    "first call mspbotsfleet_get_host to read the host's current "
                    "manual labels, then pass that full list back with your "
                    "one label added or removed."
                )
            ),
        ],
    ) -> str:
        """Replace a host's entire set of manual labels in one call — this is
        not additive. Passing a single label name deletes every other manual
        label the host had; see the labels parameter for how to add/remove
        just one without losing the rest."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.patch(f"/api/fleet/hosts/{host_id}", {"labels": labels})
        except FleetError as e:
            return e.to_envelope()
        applied = (result or {}).get("labels", labels)
        return dump_json_capped({"hostId": host_id, "labels": applied})

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def mspbotsfleet_refetch_host(
        host_id: Annotated[int, Field(description="Fleet host id.")],
    ) -> str:
        """Ask Fleet to refresh this host's properties on its next check-in.

        Asynchronous — the refresh happens whenever the host next reports
        in, not immediately.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            await client.post(f"/api/fleet/hosts/{host_id}/refetch")
        except FleetError as e:
            return e.to_envelope()
        return dump_json_capped({"hostId": host_id, "refetch": "queued"})

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def mspbotsfleet_delete_host(
        host_id: Annotated[int, Field(description="Fleet host id.")],
    ) -> str:
        """Remove a host from Fleet and clear its tenant ownership.

        If fleetd is still installed on the machine it will simply
        re-register and reappear, unowned.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            await client.delete(f"/api/fleet/hosts/{host_id}")
        except FleetError as e:
            return e.to_envelope()
        return dump_json_capped({"hostId": host_id, "deleted": True})

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def mspbotsfleet_claim_hosts(
        identifiers: Annotated[
            list[str] | None,
            Field(
                description="Hostnames, hardware serials, or UUIDs to declare for "
                "this tenant, max 500. Omit or pass an empty list to just retry "
                "identifiers declared on a previous call."
            ),
        ] = None,
    ) -> str:
        """Declare identifiers for this tenant and claim any unowned Fleet
        hosts that match, now or later.

        Idempotent — call again anytime to retry identifiers that haven't
        matched a host yet. Returns claimed/waiting/rejected: waiting means
        the identifier is declared but the host hasn't shown up in Fleet
        yet; rejected means another tenant already declared it first.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.post(
                "/api/fleet/hosts/claim", {"identifiers": identifiers or []}
            )
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def mspbotsfleet_release_host(
        host_id: Annotated[int, Field(description="Fleet host id.")],
    ) -> str:
        """Give up this tenant's ownership of a host without deleting it
        from Fleet.

        Also clears the matching declared identifier, so a future
        mspbotsfleet_claim_hosts call won't immediately re-claim it.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            await client.delete(f"/api/fleet/hosts/{host_id}/claim")
        except FleetError as e:
            return e.to_envelope()
        return dump_json_capped({"hostId": host_id, "released": True})
