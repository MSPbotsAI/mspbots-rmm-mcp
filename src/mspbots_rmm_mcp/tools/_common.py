from .._json import error_envelope

NO_TOKEN = error_envelope(
    "not_configured",
    "No RMM Control credentials. Send the X-MSP-Token, X-MSP-Tenant-Id, and X-MSP-Host headers.",
    False,
)

CONNECTION_DESC = (
    "RMM connection UUID. Omit to use the tenant's only connection — pass "
    "it explicitly only when the tenant has connected more than one RMM."
)
