"""Tests for GitHubAdapter._request error reporting (response body capture)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from integrations.github.github_adapter import GitHubAdapter


def _make_adapter() -> GitHubAdapter:
    adapter = GitHubAdapter.__new__(GitHubAdapter)
    adapter._owner = "acme-org"
    adapter._repo = "acme-mobile"
    adapter._token = "x"
    adapter._client = MagicMock()
    return adapter


def _http_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.github.com/repos/acme-org/acme-mobile/pulls")
    response = httpx.Response(status_code=status, content=body.encode("utf-8"), request=request)
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url '{request.url}'",
        request=request,
        response=response,
    )


@pytest.mark.asyncio
async def test_request_422_includes_response_body_in_error():
    adapter = _make_adapter()
    body = (
        '{"message":"Validation Failed",'
        '"errors":[{"resource":"PullRequest","code":"custom",'
        '"message":"A pull request already exists for acme-org:feature/x."}],'
        '"documentation_url":"https://docs.github.com/..."}'
    )
    err = _http_error(422, body)
    adapter._client.request = AsyncMock(side_effect=err)

    with patch("integrations.github.github_adapter.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(RuntimeError) as exc_info:
            await adapter._request("POST", "/repos/acme-org/acme-mobile/pulls", json={})

    msg = str(exc_info.value)
    assert "422" in msg
    assert "Validation Failed" in msg
    assert "pull request already exists" in msg


@pytest.mark.asyncio
async def test_request_401_still_raises_httpstatuserror_without_wrap():
    """Auth errors keep their original exception type — they are caught
    at a higher layer that distinguishes auth issues from other failures."""
    adapter = _make_adapter()
    err = _http_error(401, '{"message":"Bad credentials"}')

    response = err.response
    real_request = AsyncMock(return_value=response)
    adapter._client.request = real_request

    with pytest.raises(httpx.HTTPStatusError):
        await adapter._request("GET", "/user")


@pytest.mark.asyncio
async def test_request_timeout_after_retries_raises_timeout_not_runtimeerror():
    """Timeouts are not HTTPStatusError, so the diagnostic body wrap should
    not apply — they should surface as the original timeout exception."""
    adapter = _make_adapter()
    timeout = httpx.TimeoutException("network too slow")
    adapter._client.request = AsyncMock(side_effect=timeout)

    with patch("integrations.github.github_adapter.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(httpx.TimeoutException):
            await adapter._request("GET", "/some/path")
