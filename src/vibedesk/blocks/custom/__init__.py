"""Custom Blocks package: AI-authored blocks with lookahead testing and approval gate."""

from __future__ import annotations

from vibedesk.blocks.custom.loader import (
    CustomBlockLoader,
    custom_block,
    load_custom_blocks,
)
from vibedesk.blocks.custom.lookahead import (
    LookaheadError,
    test_custom_block_lookahead,
    verify_custom_block_lookahead,
)
from vibedesk.blocks.custom.registry import CustomBlockMeta, CustomBlockRegistry
from vibedesk.blocks.custom.validator import validate_custom_block_code

__all__ = [
    "CustomBlockLoader",
    "CustomBlockMeta",
    "CustomBlockRegistry",
    "LookaheadError",
    "custom_block",
    "load_custom_blocks",
    "test_custom_block_lookahead",
    "validate_custom_block_code",
    "verify_custom_block_lookahead",
]
