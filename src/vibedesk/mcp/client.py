"""HTTP client for the Vibe Desk MCP server (spec §8.2, CCC-58).

Calls the running Vibe Desk FastAPI backend over HTTP/ASGI.
Never sends the owner cookie and has no access to owner-only endpoints.
"""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError


class VibeDeskMcpClient:
    """HTTP client calling the Vibe Desk API for MCP tools."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8765",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute an HTTP request against the Vibe Desk API.

        Explicitly never includes the owner cookie.
        Converts non-2xx HTTP responses into ToolError.
        """
        # Strip leading slash to join cleanly with base_url
        endpoint = path if path.startswith("/") else f"/{path}"

        try:
            if self._client is not None:
                resp = await self._client.request(
                    method,
                    endpoint,
                    json=json,
                    params=params,
                )
                return self._handle_response(resp)

            async with httpx.AsyncClient(base_url=self.base_url) as client:
                resp = await client.request(
                    method,
                    endpoint,
                    json=json,
                    params=params,
                )
                return self._handle_response(resp)
        except httpx.ConnectError as err:
            raise ToolError(f"Vibe Desk is not running on {self.base_url}") from err

    def _handle_response(self, resp: httpx.Response) -> Any:
        if resp.is_error:
            detail = ""
            try:
                body = resp.json()
                if isinstance(body, dict) and "detail" in body:
                    detail = str(body["detail"])
                else:
                    detail = str(body)
            except Exception:
                detail = resp.text

            raise ToolError(f"API request failed with HTTP {resp.status_code}: {detail}")

        try:
            return resp.json()
        except Exception:
            return resp.text

    # --- Read Tools ---

    async def list_ideas(self) -> list[dict[str, Any]]:
        """List all Idea Families with trial counts and budgets."""
        data = await self._request("GET", "/api/families")
        return data if isinstance(data, list) else []

    async def get_verdict(self, trial_id: int) -> dict[str, Any]:
        """Get the verdict and summary for a completed Trial."""
        return await self._request("GET", f"/api/trials/{trial_id}")

    async def get_ledger_stats(self) -> dict[str, Any]:
        """Get desk-wide and per-family Trial counts and deflation inputs."""
        return await self._request("GET", "/api/ledger")

    async def list_blocks(self) -> dict[str, Any]:
        """Get the Block Library."""
        return await self._request("GET", "/api/blocks")

    async def get_trial(self, trial_id: int) -> dict[str, Any]:
        """Get a Trial summary with its Verdict Card data."""
        return await self._request("GET", f"/api/trials/{trial_id}")

    # --- Act Tools (counted in the Ledger) ---

    async def submit_idea(self, idea: str) -> dict[str, Any]:
        """Submit a new trading idea to create an Idea Family."""
        return await self._request("POST", "/api/families", json={"idea": idea})

    async def run_trial(self, hypothesis_id: int, idea: str = "") -> dict[str, Any]:
        """Run the Gauntlet on a Hypothesis. Counts as 1 Trial."""
        return await self._request(
            "POST",
            f"/api/hypotheses/{hypothesis_id}/trials",
            json={"idea": idea},
        )

    async def request_holdout(self, hypothesis_id: int, reason: str = "") -> dict[str, Any]:
        """Submit a Holdout Request through the gate in spec §6.3."""
        return await self._request(
            "POST",
            f"/api/hypotheses/{hypothesis_id}/holdout-requests",
            json={"reason": reason},
        )


__all__ = ["VibeDeskMcpClient"]
