"""Script execution and run history.

Execution is asynchronous and pull-based: the RMM hands the job to the
agent on its next check-in, so results are never in the dispatch response —
they arrive later and are read back through mspbotsrmm_list_script_runs.
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

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    async def mspbotsrmm_run_script(
        id: Annotated[str, Field(description="Script id exactly as returned by list_scripts.")],
        device_ids: Annotated[
            list[str],
            Field(
                description="Devices to run on, ids from list_devices. At least one "
                "required. Every id is verified upstream before anything is dispatched."
            ),
        ],
        confirm: Annotated[
            bool,
            Field(
                description="Default false = dry run: validates the script and every "
                "device exist upstream and returns a plain-language preview of what "
                "would happen, WITHOUT dispatching anything. Set true to actually "
                "execute. A call that omits this never runs anything."
            ),
        ] = False,
        parameters: Annotated[
            str | None,
            Field(description="Argument string appended to the invocation. Omit for none."),
        ] = None,
        connection: Annotated[str | None, Field(description=CONNECTION_DESC)] = None,
    ) -> str:
        """Execute a script on one or more machines. Destructive: the script
        runs with elevated privileges on real production endpoints and
        cannot be recalled once dispatched.

        Always call with confirm=false first to preview, unless the operator
        has already explicitly confirmed running this exact script on these
        exact devices. Read the script with mspbotsrmm_get_script first if
        you did not author it. Dispatching (confirm=true) returns
        immediately with one command id per device — output and exit codes
        are NOT in this response, poll mspbotsrmm_list_script_runs instead.
        Expect results within roughly a minute. Devices are dispatched one
        at a time: if a later device is rejected, the earlier ones are
        already running and the call still fails — compare the returned
        commandIds count against device_ids to detect a partial dispatch.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        body: dict = {"deviceIds": device_ids, "confirm": confirm}
        if parameters is not None:
            body["parameters"] = parameters
        if connection is not None:
            body["connection"] = connection
        try:
            result = await client.post(f"/api/rmm/scripts/{id}/run", body)
            return dump_json_capped(result)
        except RmmError as e:
            return e.to_envelope()

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def mspbotsrmm_list_script_runs(
        id: Annotated[str, Field(description="Script id whose run history to read.")],
        connection: Annotated[str | None, Field(description=CONNECTION_DESC)] = None,
        page: Annotated[int, Field(description="Zero-based page index.")] = 0,
        per_page: Annotated[int, Field(description="Items per page (max 200).")] = 20,
    ) -> str:
        """Read the execution history for one script, most recent first —
        one row per device per run, with exit code and captured output.

        This is how you collect the result of a mspbotsrmm_run_script call:
        poll it until every run of interest reaches a terminal status
        (succeeded, failed, completed_unknown, or expired — never wait on
        queued/dispatched/running to resolve further on their own within a
        single call). Any non-terminal run is reconciled against the RMM
        before the response is built, so results are current even if
        webhook delivery is broken. Check matchConfidence on each row:
        "heuristic" means the result was matched by device/script/time
        window rather than the RMM's own execution id — treat that output
        as probable, not certain, and say so when reporting it.
        """
        client = client_factory()
        if client is None:
            return NO_TOKEN
        per_page = min(per_page, _MAX_PAGE_SIZE)
        try:
            result = await client.get(
                f"/api/rmm/scripts/{id}/runs",
                params={"connection": connection, "page": page, "per_page": per_page},
            )
            return dump_json_capped(result)
        except RmmError as e:
            return e.to_envelope()
