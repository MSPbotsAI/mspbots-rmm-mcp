"""tools/list snapshot + error-envelope mapping tests.

No network calls: tool enumeration goes through FastMCP's in-process
list_tools(), and the error-code mapping is tested directly against
FleetError, independent of any real HTTP request.
"""

import pytest

from mspbots_fleet_mcp.api_client import FleetError
from mspbots_fleet_mcp.config import Settings
from mspbots_fleet_mcp.server import create_mcp_server

# name -> (required params, expected annotation hint set to True)
EXPECTED_TOOLS = {
    # hosts
    "mspbotsfleet_list_hosts": (set(), {"readOnlyHint"}),
    "mspbotsfleet_get_host": ({"host_id"}, {"readOnlyHint"}),
    "mspbotsfleet_list_host_scripts": ({"host_id"}, {"readOnlyHint"}),
    "mspbotsfleet_set_host_labels": ({"host_id", "labels"}, {"idempotentHint"}),
    "mspbotsfleet_refetch_host": ({"host_id"}, {"idempotentHint"}),
    "mspbotsfleet_delete_host": ({"host_id"}, {"destructiveHint"}),
    "mspbotsfleet_claim_hosts": (set(), {"idempotentHint"}),
    # scripts
    "mspbotsfleet_list_scripts": (set(), {"readOnlyHint"}),
    "mspbotsfleet_get_script": ({"script_id"}, {"readOnlyHint"}),
    "mspbotsfleet_create_script": ({"name", "contents"}, {"idempotentHint"}),
    "mspbotsfleet_update_script": ({"script_id", "contents"}, {"idempotentHint"}),
    "mspbotsfleet_delete_script": ({"script_id"}, {"destructiveHint"}),
    "mspbotsfleet_run_script": ({"script_id", "host_id"}, {"destructiveHint"}),
    "mspbotsfleet_get_script_result": ({"execution_id"}, {"readOnlyHint"}),
    # queries
    "mspbotsfleet_list_queries": (set(), {"readOnlyHint"}),
    "mspbotsfleet_get_query": ({"query_id"}, {"readOnlyHint"}),
    "mspbotsfleet_create_query": ({"name", "sql"}, {"idempotentHint"}),
    "mspbotsfleet_update_query": ({"query_id"}, {"idempotentHint"}),
    "mspbotsfleet_delete_query": ({"query_id"}, {"destructiveHint"}),
    "mspbotsfleet_run_query": ({"query_id"}, {"idempotentHint"}),
}


@pytest.mark.asyncio
async def test_tools_list_snapshot():
    mcp = create_mcp_server(Settings())
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == set(EXPECTED_TOOLS), f"unexpected tool set: {names}"

    by_name = {t.name: t for t in tools}
    for name, (expected_required, expected_hints) in EXPECTED_TOOLS.items():
        tool = by_name[name]
        required = set(tool.inputSchema.get("required", []))
        assert required == expected_required, f"{name}: required={required}"

        description = tool.description or ""
        assert len(description) <= 500, f"{name}: description too long ({len(description)})"
        first_line = description.strip().splitlines()[0] if description.strip() else ""
        assert len(first_line) <= 100, f"{name}: first line too long: {first_line!r}"
        assert "API:" not in description, f"{name}: leaked implementation detail"
        assert "GET /" not in description and "POST /" not in description, (
            f"{name}: leaked implementation detail"
        )

        annotations = tool.annotations
        actual_hints = set()
        if annotations is not None:
            for hint in ("readOnlyHint", "destructiveHint", "idempotentHint"):
                if getattr(annotations, hint, None) is True:
                    actual_hints.add(hint)
        assert actual_hints == expected_hints, f"{name}: hints={actual_hints}"


@pytest.mark.asyncio
async def test_service_instructions_present_and_bounded():
    mcp = create_mcp_server(Settings())
    assert mcp.instructions
    assert len(mcp.instructions) <= 1500


@pytest.mark.parametrize(
    "status_code,expected_code,expected_retryable",
    [
        (0, "upstream_error", True),
        (400, "invalid_argument", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (409, "conflict", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (502, "upstream_error", True),
        (503, "upstream_error", True),
    ],
)
def test_error_envelope_mapping(status_code, expected_code, expected_retryable):
    import json

    err = FleetError(status_code, "boom")
    envelope = json.loads(err.to_envelope())
    assert envelope["error"]["code"] == expected_code
    assert envelope["error"]["retryable"] is expected_retryable
    assert envelope["error"]["message"] == "boom"
