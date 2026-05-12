"""Tests for GitLabAdapter._request — retries, auth fail, body capture."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from integrations.gitlab.gitlab_adapter import GitLabAdapter


def _make_adapter() -> GitLabAdapter:
    adapter = GitLabAdapter.__new__(GitLabAdapter)
    adapter._token = "t"
    adapter._project_id = "group/proj"
    adapter._url = "https://gitlab.com"
    adapter._project_path = "group%2Fproj"
    adapter._client = MagicMock()
    adapter._discussion_cache = {}
    return adapter


def _http_error(status: int, body: str) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "POST",
        "https://gitlab.com/api/v4/projects/group%2Fproj/merge_requests",
    )
    response = httpx.Response(
        status_code=status, content=body.encode("utf-8"), request=request,
    )
    return httpx.HTTPStatusError(
        f"Client error '{status}' for url '{request.url}'",
        request=request, response=response,
    )


@pytest.mark.asyncio
async def test_request_400_includes_response_body_in_error():
    adapter = _make_adapter()
    body = '{"message":"Branch \\"feature/x\\" already exists"}'
    err = _http_error(400, body)
    adapter._client.request = AsyncMock(side_effect=err)

    with patch("integrations.gitlab.gitlab_adapter.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(RuntimeError) as exc_info:
            await adapter._request("POST", "/projects/group%2Fproj/merge_requests", json={})

    msg = str(exc_info.value)
    assert "400" in msg
    assert "already exists" in msg


@pytest.mark.asyncio
async def test_request_401_raises_httpstatuserror_without_wrap():
    adapter = _make_adapter()
    err = _http_error(401, '{"message":"401 Unauthorized"}')
    response = err.response
    adapter._client.request = AsyncMock(return_value=response)

    with pytest.raises(httpx.HTTPStatusError):
        await adapter._request("GET", "/user")


@pytest.mark.asyncio
async def test_request_timeout_after_retries_raises_timeout():
    adapter = _make_adapter()
    timeout = httpx.TimeoutException("network too slow")
    adapter._client.request = AsyncMock(side_effect=timeout)

    with patch("integrations.gitlab.gitlab_adapter.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(httpx.TimeoutException):
            await adapter._request("GET", "/some/path")


@pytest.mark.asyncio
async def test_request_204_returns_empty_dict():
    adapter = _make_adapter()
    response = MagicMock()
    response.status_code = 204
    response.raise_for_status = MagicMock()
    adapter._client.request = AsyncMock(return_value=response)

    result = await adapter._request("DELETE", "/some/path")
    assert result == {}
