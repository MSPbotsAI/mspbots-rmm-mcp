from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .._json import dump_json_capped
from ..api_client import FleetClient, FleetError
from ._common import NO_TOKEN

_MAX_PAGE_SIZE = 200
_MAX_LIVE_QUERY_HOSTS = 50


def register(mcp: FastMCP, client_factory: Callable[[], FleetClient | None]) -> None:

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsfleet_list_queries(
        page: Annotated[int, Field(description="0-based page number.")] = 0,
        per_page: Annotated[int, Field(description="Rows per page (max 200).")] = 25,
    ) -> str:
        """List this tenant's saved queries, by name."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        per_page = min(per_page, _MAX_PAGE_SIZE)
        try:
            result = await client.get(
                "/api/fleet/queries", params={"page": page, "per_page": per_page}
            )
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsfleet_get_query(
        query_id: Annotated[int, Field(description="Fleet query id.")],
    ) -> str:
        """Get a saved query's full details, including its SQL text."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.get(f"/api/fleet/queries/{query_id}")
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool()
    async def mspbotsfleet_create_query(
        name: Annotated[str, Field(description="Query name, non-empty, ≤255 chars.")],
        sql: Annotated[
            str,
            Field(
                description="A single osquery SELECT (or WITH … SELECT). No "
                "trailing semicolon; a semicolon anywhere in the body is rejected."
            ),
        ],
        description: Annotated[str | None, Field(description="Human-readable summary.")] = None,
        platform: Annotated[
            str | None, Field(description='"darwin" | "windows" | "linux"; omit for all.')
        ] = None,
    ) -> str:
        """Create a new saved osquery SELECT in this tenant's library.

        Not idempotent — calling this again with the same name creates
        another, separate query rather than updating the existing one.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body = {"name": name, "sql": sql}
        if description is not None:
            body["description"] = description
        if platform is not None:
            body["platform"] = platform
        try:
            result = await client.post("/api/fleet/queries", body)
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def mspbotsfleet_update_query(
        query_id: Annotated[int, Field(description="Fleet query id.")],
        name: Annotated[str | None, Field(description="New name.")] = None,
        sql: Annotated[str | None, Field(description="New SQL. Same rules as create.")] = None,
        description: Annotated[str | None, Field(description="New description.")] = None,
        platform: Annotated[str | None, Field(description="New platform filter.")] = None,
    ) -> str:
        """Update a saved query (partial — only send the fields you want to
        change)."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body = {}
        for key, value in (
            ("name", name),
            ("sql", sql),
            ("description", description),
            ("platform", platform),
        ):
            if value is not None:
                body[key] = value
        if not body:
            return "Error: nothing to update — provide at least one field to change"
        try:
            result = await client.patch(f"/api/fleet/queries/{query_id}", body)
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def mspbotsfleet_delete_query(
        query_id: Annotated[int, Field(description="Fleet query id.")],
    ) -> str:
        """Delete a saved query and any report data Fleet kept for it."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            await client.delete(f"/api/fleet/queries/{query_id}")
        except FleetError as e:
            return e.to_envelope()
        return dump_json_capped({"queryId": query_id, "deleted": True})

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def mspbotsfleet_run_query(
        query_id: Annotated[int, Field(description="Saved query to run live.")],
        host_ids: Annotated[
            list[int] | None,
            Field(
                description="Target hosts, max 50, all must belong to this "
                "tenant. Omit to default to this tenant's first 50 hosts."
            ),
        ] = None,
    ) -> str:
        """Run a saved query live against hosts and wait for results
        (up to 45s). Hosts that don't answer in time show up as an
        error entry rather than blocking the others."""
        client = client_factory()
        if client is None:
            return NO_TOKEN
        if host_ids is not None and len(host_ids) > _MAX_LIVE_QUERY_HOSTS:
            return (
                f"Error: host_ids has {len(host_ids)} entries, max is "
                f"{_MAX_LIVE_QUERY_HOSTS}"
            )
        body = {}
        if host_ids is not None:
            body["hostIds"] = host_ids
        try:
            result = await client.post(f"/api/fleet/queries/{query_id}/run", body)
            return dump_json_capped(result)
        except FleetError as e:
            return e.to_envelope()
