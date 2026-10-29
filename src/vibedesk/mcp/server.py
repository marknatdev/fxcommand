"""Stdio MCP server for Vibe Desk (spec §8.2, CCC-58).

Exposes 5 read tools and 3 act tools that communicate with the running
HTTP API on 127.0.0.1 over httpx.

Read tools:
- list_ideas: GET /api/families
- get_verdict: GET /api/trials/{id}
- get_ledger_stats: GET /api/ledger
- list_blocks: GET /api/blocks
- get_trial: GET /api/trials/{id}

Act tools (counted in the Ledger through the API):
- submit_idea: POST /api/families
- run_trial: POST /api/hypotheses/{id}/trials
- request_holdout: POST /api/hypotheses/{id}/holdout-requests

Has NO approve, budget or settings tools and never sends the owner cookie.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from vibedesk.mcp.client import VibeDeskMcpClient

DEFAULT_BASE_URL = "http://127.0.0.1:8765"


def create_mcp_server(
    base_url: str | None = None,
    client: httpx.AsyncClient | None = None,
    api_client: VibeDeskMcpClient | None = None,
) -> MCPServer:
    """Create and configure the Vibe Desk MCPServer instance.

    Args:
        base_url: Base URL for Vibe Desk HTTP API (defaults to $VIBEDESK_API_URL
            or 127.0.0.1:8765).
        client: Optional pre-configured httpx.AsyncClient (e.g. ASGITransport).
        api_client: Optional pre-configured VibeDeskMcpClient.
    """
    effective_url = base_url or os.environ.get("VIBEDESK_API_URL", DEFAULT_BASE_URL)
    backend = api_client or VibeDeskMcpClient(base_url=effective_url, client=client)

    server = MCPServer(
        name="vibedesk-mcp",
        version="0.1.0",
        instructions=(
            "Vibe Desk research tools for ideas, blocks, trials, verdicts, and ledger stats. "
            "Layer A never trades live and has no order execution capability."
        ),
    )

    # --- Read Tools (spec §8.2) ---

    @server.tool()
    async def list_ideas() -> list[dict[str, Any]]:
        """List all Idea Families with their trial counts and budgets."""
        return await backend.list_ideas()

    @server.tool()
    async def get_verdict(trial_id: int | None = None, id: int | None = None) -> dict[str, Any]:
        """Get the verdict and summary for a completed Trial.

        Args:
            trial_id: The ID of the Trial (also accepts id).
            id: Alias for trial_id.
        """
        tid = trial_id if trial_id is not None else id
        if tid is None:
            raise ToolError("trial_id (or id) is required")
        return await backend.get_verdict(tid)

    @server.tool()
    async def get_ledger_stats() -> dict[str, Any]:
        """Get desk-wide and per-family Trial counts and deflation inputs."""
        return await backend.get_ledger_stats()

    @server.tool()
    async def list_blocks() -> dict[str, Any]:
        """Get the Block Library (all available entry, filter, and exit blocks)."""
        return await backend.list_blocks()

    @server.tool()
    async def get_trial(trial_id: int | None = None, id: int | None = None) -> dict[str, Any]:
        """Get a Trial's full summary and Verdict Card data.

        Args:
            trial_id: The ID of the Trial (also accepts id).
            id: Alias for trial_id.
        """
        tid = trial_id if trial_id is not None else id
        if tid is None:
            raise ToolError("trial_id (or id) is required")
        return await backend.get_trial(tid)

    # --- Act Tools (spec §8.2, counted in the Ledger) ---

    @server.tool()
    async def submit_idea(idea: str) -> dict[str, Any]:
        """Submit a new trading idea to create an Idea Family.

        Args:
            idea: Plain text description of the trading idea.
        """
        if not idea or not idea.strip():
            raise ToolError("idea must not be empty")
        return await backend.submit_idea(idea.strip())

    @server.tool()
    async def run_trial(
        hypothesis_id: int | None = None,
        id: int | None = None,
        idea: str = "",
    ) -> dict[str, Any]:
        """Run the Gauntlet on a Hypothesis. Counts as 1 Trial in the Ledger.

        Args:
            hypothesis_id: The ID of the Hypothesis to test (also accepts id).
            id: Alias for hypothesis_id.
            idea: Optional label or note for this trial run.
        """
        hid = hypothesis_id if hypothesis_id is not None else id
        if hid is None:
            raise ToolError("hypothesis_id (or id) is required")
        return await backend.run_trial(hid, idea=idea)

    @server.tool()
    async def request_holdout(
        hypothesis_id: int | None = None,
        id: int | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        """Submit a Holdout Request for a CANDIDATE Hypothesis through the gate in spec §6.3.

        Args:
            hypothesis_id: The ID of the Hypothesis requesting Holdout (also accepts id).
            id: Alias for hypothesis_id.
            reason: Optional explanation or note for the request.
        """
        hid = hypothesis_id if hypothesis_id is not None else id
        if hid is None:
            raise ToolError("hypothesis_id (or id) is required")
        return await backend.request_holdout(hid, reason=reason)

    return server


def main(argv: list[str] | None = None) -> None:
    """Entry point for the vibedesk-mcp console script (stdio transport)."""
    parser = argparse.ArgumentParser(
        prog="vibedesk-mcp",
        description="Vibe Desk MCP Server (stdio transport over HTTP API)",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"Vibe Desk HTTP API base URL (default: {DEFAULT_BASE_URL} or $VIBEDESK_API_URL)",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio"],
        help="Transport protocol (default: stdio)",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    server = create_mcp_server(base_url=args.base_url)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
