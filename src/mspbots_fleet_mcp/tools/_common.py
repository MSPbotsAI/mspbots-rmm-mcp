from .._json import error_envelope

NO_TOKEN = error_envelope(
    "not_configured",
    "No Fleet Platform credentials. Send the X-MSP-Token, X-MSP-Tenant-Id, and X-MSP-Host headers.",
    False,
)
