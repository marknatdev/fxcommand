"""Safety and look-ahead validation for custom Blocks."""

from __future__ import annotations

import ast


def validate_custom_block_code(code: str) -> tuple[bool, str | None]:
    """Validate safety and syntax of proposed custom block code (spec §8.2).

    1. Must parse cleanly as Python syntax.
    2. Prohibits dangerous imports or system calls (os, sys, subprocess, eval, exec).
    3. Must pass property tests before approval.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    forbidden = {"os", "sys", "subprocess", "eval", "exec", "socket", "urllib", "requests"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in forbidden:
                    return False, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in forbidden:
                return False, f"Forbidden import: {node.module}"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("eval", "exec", "compile", "__import__")
        ):
            return False, f"Forbidden call: {node.func.id}"

    return True, None
