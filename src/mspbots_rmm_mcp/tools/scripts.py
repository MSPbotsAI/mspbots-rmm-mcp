"""Script library tools.

Scripts live in the connected RMM, not in this app — every write here is a
pass-through with no local copy and no sync step, so a change is
immediately visible to anyone using the RMM directly. Not every connected
vendor allows writes to its library; create/update/delete return a
permanent, non-retryable error naming the vendor when unsupported.
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

_NAME_DESC = (
    "File name including extension, e.g. \"collect-logs.sh\". The extension "
    "selects the interpreter and is validated against the connected vendor's "
    "own rules (e.g. Fleet: .sh optional shell shebang, .py requires "
    '"#!/usr/bin/env python3", .ps1 must not have a shebang at all) — read '
    "the live rules from mspbotsrmm_list_devices' sibling vendor-metadata "
    "endpoint if unsure, don't hard-code them."
)
_CONTENTS_DESC = "Full script source, non-empty."


def register(mcp: FastMCP, client_factory: Callable[[], RmmClient | None]) -> None:

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsrmm_list_scripts(
        connection: Annotated[str | None, Field(description=CONNECTION_DESC)] = None,
        page: Annotated[int, Field(description="Zero-based page index.")] = 0,
        per_page: Annotated[int, Field(description="Items per page (max 200).")] = 25,
    ) -> str:
        """List the tenant's script library with the outcome of each
        script's most recent run.

        Use it to find a script id before executing one, or to see at a
        glance which scripts last failed. The last-run columns are computed
        from local command history, so a script run directly in the RMM
        console shows as never run here.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        per_page = min(per_page, _MAX_PAGE_SIZE)
        try:
            result = await client.get(
                "/api/rmm/scripts",
                params={"connection": connection, "page": page, "per_page": per_page},
            )
            return dump_json_capped(result)
        except RmmError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsrmm_get_script(
        id: Annotated[str, Field(description="Script id exactly as returned by list_scripts.")],
        connection: Annotated[str | None, Field(description=CONNECTION_DESC)] = None,
    ) -> str:
        """Read one script including its full source.

        Use it to review what a script actually does before running it on
        production machines, or to fetch the current text before editing —
        mspbotsrmm_update_script overwrites the whole document, so fetch
        first unless deliberately replacing it wholesale.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            result = await client.get(f"/api/rmm/scripts/{id}", params={"connection": connection})
            return dump_json_capped(result)
        except RmmError as e:
            return e.to_envelope()

    @mcp.tool()
    async def mspbotsrmm_create_script(
        name: Annotated[str, Field(description=_NAME_DESC)],
        contents: Annotated[str, Field(description=_CONTENTS_DESC)],
        connection: Annotated[str | None, Field(description=CONNECTION_DESC)] = None,
    ) -> str:
        """Upload a new script to the connected RMM's library.

        The file name is not cosmetic — get the extension/shebang wrong and
        the call fails with the vendor's own validation message. Returns the
        new script's vendor-native id, which is what mspbotsrmm_run_script
        expects. Not idempotent — calling again with the same name creates
        another, separate script. Some RMMs expose a read-only library and
        return a permanent error for this call.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body: dict = {"name": name, "contents": contents}
        if connection is not None:
            body["connection"] = connection
        try:
            result = await client.post("/api/rmm/scripts", body)
            return dump_json_capped(result)
        except RmmError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(idempotentHint=True))
    async def mspbotsrmm_update_script(
        id: Annotated[str, Field(description="Script id exactly as returned by list_scripts.")],
        name: Annotated[str, Field(description=_NAME_DESC)],
        contents: Annotated[str, Field(description=_CONTENTS_DESC)],
        connection: Annotated[str | None, Field(description=CONNECTION_DESC)] = None,
    ) -> str:
        """Replace a script's name and contents — a whole-document write,
        not a partial patch.

        Send the full source every time, including parts you are not
        changing; fetch the current text with mspbotsrmm_get_script first
        unless deliberately overwriting it. The new name is validated
        against the same extension rules as creation. Existing run history
        stays keyed by this script id and survives the edit, so history
        will mix results from before and after the change.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body: dict = {"name": name, "contents": contents}
        if connection is not None:
            body["connection"] = connection
        try:
            result = await client.patch(f"/api/rmm/scripts/{id}", body)
            return dump_json_capped(result)
        except RmmError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def mspbotsrmm_delete_script(
        id: Annotated[str, Field(description="Script id exactly as returned by list_scripts.")],
        connection: Annotated[str | None, Field(description=CONNECTION_DESC)] = None,
    ) -> str:
        """Remove a script from the RMM library permanently.

        Destructive and not undoable — the source is gone from the RMM
        itself, and anyone else using that RMM loses it too. Local run
        history is not deleted: past runs remain queryable by this script id
        even after it no longer exists. Confirm with a human before calling
        — there is no dry-run mode on this endpoint.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        try:
            await client.delete(f"/api/rmm/scripts/{id}", params={"connection": connection})
        except RmmError as e:
            return e.to_envelope()
        return dump_json_capped({"id": id, "deleted": True})
