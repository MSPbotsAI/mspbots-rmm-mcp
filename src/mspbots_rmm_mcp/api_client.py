import asyncio
from typing import Any

import httpx

from ._json import error_envelope

# The RMM Control app is mounted at this path prefix on the tenant host:
# "https://<host>/apps/mb-platform-rmm/<sub-path>".
# X-MSP-Host only carries the bare host; do not hardcode the prefix elsewhere.
# Callers pass the full sub-path below this prefix, e.g.
#   "/api/rmm/devices", "/api/rmm/scripts/<id>/run".
_APP_PREFIX = "/apps/mb-platform-rmm"

# read=65s: run_script's dispatch itself is quick (async, pull-based), but
# some upstream RMM calls (e.g. script validation) can be slow.
_TIMEOUT = httpx.Timeout(connect=5.0, read=65.0, write=10.0, pool=5.0)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_MAX_BACKOFF_SECONDS = 20.0

# One shared connection pool for the process lifetime. No credentials are
# ever stored on it — the bearer token/tenant id/host are passed per-request
# via headers, so this is safe to share across tenants/requests (see
# server.py's contextvar-based credential isolation, which is what actually
# keeps tenants apart).
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True)
    return _http_client


# status_code -> (error code, retryable). status_code 0 means a network/
# connection-level failure (no response at all). 501 is the RMM Control
# API's capability-gating status — permanent for that connection, must
# never be retried (see README "Capability gating").
_STATUS_TO_CODE: dict[int, tuple[str, bool]] = {
    0: ("upstream_error", True),
    400: ("invalid_argument", False),
    # 401 and 403 both mean "your credential did not get you in". Without the
    # 401 entry it fell through to the invalid_argument default, so an expired
    # or unknown API key reached the agent as a bad-parameter error and the
    # agent would tell the user to fix their arguments instead of to
    # re-authorize.
    401: ("unauthorized", False),
    403: ("unauthorized", False),
    404: ("not_found", False),
    429: ("rate_limited", True),
    501: ("not_supported", False),
    502: ("upstream_error", True),
}


def _classify(status_code: int) -> tuple[str, bool]:
    if status_code in _STATUS_TO_CODE:
        return _STATUS_TO_CODE[status_code]
    if status_code >= 500:
        return "upstream_error", True
    return "invalid_argument", False


class RmmError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"RMM Control API error {status_code}: {message}")

    def to_envelope(self) -> str:
        code, retryable = _classify(self.status_code)
        return error_envelope(code, self.message, retryable)


class RmmClient:
    """Async httpx client wrapping the MSPbots RMM Control API
    (`/apps/mb-platform-rmm/api/rmm/*`).

    Reuses the module-level connection pool (see _get_http_client) across
    every call made through this instance, rather than opening a new
    connection per request.

    The credential is an opaque platform-issued API key (`mbk_...`), not a
    JWT: nothing is embedded in it, so the tenant id always travels separately
    as an `X_Tenant_ID` header, per the platform convention (also relied on by
    the sibling agent/forms/ticketqa services).

    Note: the RMM Control API answers any authentication failure with
    `{"code": 401, "message": "Unauthorized"}` — missing key, unknown key, or
    insufficient role all collapse to that one shape, so the status code alone
    never tells you which. That's the upstream contract, not a bug in this
    client. It returns 501 (not a generic 4xx) when the connected RMM vendor
    doesn't support the requested capability — see README "Capability gating".
    """

    def __init__(self, access_token: str, host: str, tenant_id: str):
        self._token = access_token
        self._tenant_id = tenant_id
        self._base_url = host.rstrip("/") + _APP_PREFIX

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self._token,
            # NOTE(transition, 2026-09-23): the RMM Control API moved from
            # `Authorization: Bearer` to `X-API-Key` on the same day this
            # server's own inbound header was renamed. Both are sent while the
            # two sides land, because a wrong guess here is indistinguishable
            # from an expired key: every call just returns 401. Drop the
            # Authorization line once the API is confirmed to read X-API-Key,
            # together with the inbound X-MSP-Token fallback in server.py.
            "Authorization": f"Bearer {self._token}",
            "X_Tenant_ID": self._tenant_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _clean_params(self, params: dict | None) -> dict:
        if not params:
            return {}
        return {k: v for k, v in params.items() if v is not None}

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json_body: Any = None) -> Any:
        return await self._request("POST", path, json_body=json_body)

    async def patch(self, path: str, json_body: Any) -> Any:
        return await self._request("PATCH", path, json_body=json_body)

    async def delete(self, path: str, params: dict | None = None) -> Any:
        return await self._request("DELETE", path, params=params)

    async def _request(
        self, method: str, path: str, params: dict | None = None, json_body: Any = None
    ) -> Any:
        client = _get_http_client()
        url = f"{self._base_url}{path}"
        headers = self._headers()
        params = self._clean_params(params)

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = await client.request(
                    method, url, headers=headers, params=params, json=json_body
                )
            except httpx.RequestError as e:
                last_exc = e
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(min(2**attempt, _MAX_BACKOFF_SECONDS))
                    continue
                raise RmmError(0, f"{e or type(e).__name__} (url={url})") from e

            if resp.status_code in _RETRYABLE_STATUS and attempt < _MAX_RETRIES:
                delay = self._retry_delay(resp, attempt)
                await asyncio.sleep(delay)
                continue

            return self._handle(resp)

        # Unreachable in practice (loop always returns or raises above), but
        # keeps type checkers happy and guards against future edits.
        if last_exc:
            raise RmmError(0, f"{last_exc}") from last_exc
        raise RmmError(0, "request failed with no response")

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), _MAX_BACKOFF_SECONDS)
            except ValueError:
                pass
        return min(2**attempt, _MAX_BACKOFF_SECONDS)

    def _handle(self, resp: httpx.Response) -> Any:
        if not resp.content:
            return None
        try:
            body = resp.json()
        except ValueError:
            body = {"raw_response": resp.text}
        if resp.status_code >= 400:
            # The RMM Control API's error envelope is {message, detail, code}.
            # `detail` is deliberately not folded into the message when it's
            # the raw upstream response body — that can be arbitrary/
            # unbounded content (SOP §4.2: never dump a full API response
            # into a tool-visible error message). A short, non-null detail
            # is appended since it's usually the more specific cause.
            if isinstance(body, dict):
                message = body.get("message") or "unknown error"
                detail = body.get("detail")
                if isinstance(detail, str) and detail and len(detail) < 200:
                    message = f"{message} ({detail})"
            else:
                message = str(body)
            raise RmmError(resp.status_code, message)
        return body
