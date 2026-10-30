"""Custom Blocks package: AI-authored blocks with lookahead testing and approval gate."""

from __future__ import annotations

from vibedesk.blocks.custom.registry import CustomBlockMeta, CustomBlockRegistry
from vibedesk.blocks.custom.validator import validate_custom_block_code

__all__ = [
    "CustomBlockMeta",
    "CustomBlockRegistry",
    "validate_custom_block_code",
]
