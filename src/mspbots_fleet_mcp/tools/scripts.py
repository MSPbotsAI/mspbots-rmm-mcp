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
    async def mspbotsfleet_list_scripts(
        page: Annotated[int, Field(description="0-based page number.")] = 0,
        per_page: Annotated[int, Field(description="Rows per page (max 200).")] = 25,
    ) -> str:
        """List this tenant's scripts, by name."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        per_page = min(per_page, _MAX_PAGE_SIZE)
        try:
            result = await client.get(
                "/api/fleet/scripts", params={"page": page, "per_page": per_page}
            )
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsfleet_get_script(
        script_id: Annotated[int, Field(description="Fleet script id.")],
    ) -> str:
        """Get a script's full details, including its contents."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.get(f"/api/fleet/scripts/{script_id}")
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool()
    async def mspbotsfleet_create_script(
        name: Annotated[
            str, Field(description='File name, must end with ".sh" or ".ps1".')
        ],
        contents: Annotated[
            str, Field(description="Script body, non-empty, UTF-8 ≤ 500,000 bytes.")
        ],
    ) -> str:
        """Create a new script in this tenant's library.

        Not idempotent — calling this again with the same name creates
        another, separate script rather than updating the existing one.
        Returns only {id, name}; fetch mspbotsfleet_get_script for the rest.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.post("/api/fleet/scripts", {"name": name, "contents": contents})
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def mspbotsfleet_update_script(
        script_id: Annotated[int, Field(description="Fleet script id.")],
        contents: Annotated[str, Field(description="New script body (replaces it in full).")],
    ) -> str:
        """Replace a script's contents. The file name cannot be changed.
        Returns only {id, name}."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.patch(f"/api/fleet/scripts/{script_id}", {"contents": contents})
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def mspbotsfleet_delete_script(
        script_id: Annotated[int, Field(description="Fleet script id.")],
    ) -> str:
        """Permanently remove this script from the tenant's library and from
        every host that could run it. Irreversible — there is no undo, and a
        host with this script currently running or queued loses access to it
        mid-flight."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            await client.delete(f"/api/fleet/scripts/{script_id}")
        except FleetError as e:
            return e.to_envelope()
        return dump_json_capped({"scriptId": script_id, "deleted": True})

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def mspbotsfleet_run_script(
        script_id: Annotated[int, Field(description="Script to run.")],
        host_id: Annotated[
            int, Field(description="Target host — must belong to this tenant.")
        ],
        sync: Annotated[
            bool,
            Field(
                description="Wait for the host to finish (up to 60s) and include "
                "the result inline, instead of returning immediately."
            ),
        ] = False,
    ) -> str:
        """Run a script on a host. Runs arbitrary code on that machine —
        treat as destructive even though Fleet itself doesn't undo it.
        Runs once, on demand — there is no recurring/scheduled execution
        here; to run it again later, call this again at that time.

        With sync=false (default) this returns an executionId to poll via
        mspbotsfleet_get_script_result; with sync=true it waits and returns
        the result inline.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.post(
                f"/api/fleet/scripts/{script_id}/run", {"hostId": host_id, "sync": sync}
            )
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsfleet_get_script_result(
        execution_id: Annotated[str, Field(description="Execution id from run_script.")],
    ) -> str:
        """Get a script run's result. exit_code is null if the host hasn't
        reported back yet — keep polling."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.get(f"/api/fleet/scripts/results/{execution_id}")
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()
