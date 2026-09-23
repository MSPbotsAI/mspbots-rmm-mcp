"""tools/list snapshot + error-envelope mapping tests.

No network calls: tool enumeration goes through FastMCP's in-process
list_tools(), and the error-code mapping is tested directly against
RmmError, independent of any real HTTP request.
"""

import pytest

from mspbots_rmm_mcp.api_client import RmmError
from mspbots_rmm_mcp.config import Settings
from mspbots_rmm_mcp.server import create_mcp_server

# name -> (required params, expected annotation hint set to True)
EXPECTED_TOOLS = {
    # devices
    "mspbotsrmm_list_devices": (set(), {"readOnlyHint"}),
    "mspbotsrmm_get_device": ({"id"}, {"readOnlyHint"}),
    "mspbotsrmm_refetch_device": ({"id"}, {"idempotentHint"}),
    # scripts
    "mspbotsrmm_list_scripts": (set(), {"readOnlyHint"}),
    "mspbotsrmm_get_script": ({"id"}, {"readOnlyHint"}),
    "mspbotsrmm_create_script": ({"name", "contents"}, set()),
    "mspbotsrmm_update_script": ({"id", "name", "contents"}, {"idempotentHint"}),
    "mspbotsrmm_delete_script": ({"id"}, {"destructiveHint"}),
    # execution
    "mspbotsrmm_run_script": ({"id", "device_ids"}, {"destructiveHint"}),
    "mspbotsrmm_list_script_runs": ({"id"}, {"readOnlyHint"}),
}

# Tools whose docstrings deliberately exceed the SOP's 500-char description
# guideline (§2.2, a "should" not a hard rule): they carry load-bearing
# guidance an agent needs to call them correctly and safely.
_LONG_DESCRIPTION_EXCEPTIONS = {
    # Destructive, dispatches real work to production endpoints — the
    # confirm=false-by-default dry-run behavior, the "poll list_script_runs
    # instead" pointer, and the partial-dispatch warning are all things an
    # agent needs to know before calling this, not decorative detail.
    "mspbotsrmm_run_script",
    # Explains matchConfidence (exact vs heuristic result matching) and the
    # terminal-status polling contract — both needed to interpret output
    # correctly, not optional color.
    "mspbotsrmm_list_script_runs",
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
        if name not in _LONG_DESCRIPTION_EXCEPTIONS:
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
async def test_reboot_device_is_not_registered():
    # Per the RMM Control API docs: the route exists but no vendor adapter
    # implements it, so it always answers 501 — must not be an MCP tool.
    mcp = create_mcp_server(Settings())
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert not any("reboot" in name for name in names), names


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
        (401, "unauthorized", False),
        (403, "unauthorized", False),
        (404, "not_found", False),
        (409, "invalid_argument", False),
        (429, "rate_limited", True),
        (500, "upstream_error", True),
        (501, "not_supported", False),
        (502, "upstream_error", True),
        (503, "upstream_error", True),
    ],
)
def test_error_envelope_mapping(status_code, expected_code, expected_retryable):
    import json

    err = RmmError(status_code, "boom")
    envelope = json.loads(err.to_envelope())
    assert envelope["error"]["code"] == expected_code
    assert envelope["error"]["retryable"] is expected_retryable
    assert envelope["error"]["message"] == "boom"


def test_downstream_headers_carry_the_key_as_x_api_key():
    """The RMM Control API reads the credential from X-API-Key.

    Pinned because getting this wrong is invisible in any local test: the API
    answers a missing key and a wrong-header key with the same 401, so a
    regression here looks exactly like an expired credential.
    """
    from mspbots_rmm_mcp.api_client import RmmClient

    client = RmmClient("mbk_test_key", "https://agentint.mspbots.ai", "tenant-123")
    headers = client._headers()

    assert headers["X-API-Key"] == "mbk_test_key"
    assert headers["X_Tenant_ID"] == "tenant-123"
    # No Authorization header: the platform standardised on the API key and
    # dropped the JWT (PRD-19165). Sending a stale Bearer alongside it would
    # give the API a second credential to prefer.
    assert "Authorization" not in headers
