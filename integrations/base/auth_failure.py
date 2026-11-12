"""The httpx response hook through which an adapter reports a 401/403 to credential health."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


def auth_failure_hook(
    on_auth_failure: Callable[[int], None] | None,
) -> Callable[[httpx.Response], Awaitable[None]]:
    """A response event hook that calls `on_auth_failure(status)` for a 401/403.

    Register it on every httpx.AsyncClient an adapter builds. A failing callback is
    logged (class name only) and swallowed; the response is never changed.
    """
    async def hook(response: httpx.Response) -> None:
        if on_auth_failure is None or response.status_code not in (401, 403):
            return
        try:
            on_auth_failure(response.status_code)
        except Exception as e:
            logger.warning("on_auth_failure callback failed: %s", type(e).__name__)

    return hook
