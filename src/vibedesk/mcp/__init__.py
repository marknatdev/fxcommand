"""Vibe Desk MCP Server package (spec §8.2, CCC-58).

Exposes stdio MCP tools calling the running Vibe Desk HTTP API.
"""

from __future__ import annotations

from vibedesk.mcp.client import VibeDeskMcpClient
from vibedesk.mcp.server import create_mcp_server, main

__all__ = [
    "VibeDeskMcpClient",
    "create_mcp_server",
    "main",
]
