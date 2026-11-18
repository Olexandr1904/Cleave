"""Dashboard route for the Comparison section: every comparison and the tally of verdicts. Read-only."""
from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def build_comparison_routes(board: Any) -> list[Route]:
    async def get_comparisons(request: Request) -> JSONResponse:
        return JSONResponse(board.overview())

    return [Route("/api/comparisons", get_comparisons, methods=["GET"])]
