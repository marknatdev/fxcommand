"""Safety and syntax validation for custom Blocks."""

from __future__ import annotations

import ast

ALLOWED_MODULES = {
    "numpy",
    "math",
    "pydantic",
    "vibedesk.blocks.indicators",
    "vibedesk.blocks.registry",
    "vibedesk.market.bars",
    "vibedesk.blocks.custom",
    "__future__",
}

FORBIDDEN_CALLS: set[str] = {
    "getattr",
    "setattr",
    "globals",
    "vars",
    "locals",
    "delattr",
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",
}


def validate_custom_block_code(code: str) -> tuple[bool, str | None]:
    """Validate safety and syntax of proposed custom block code (spec §8.2, ADR 0012).

    1. Must parse cleanly as Python syntax.
    2. Prohibits dangerous imports (os, sys, subprocess, network, MT5).
    3. Prohibits dangerous calls (eval, exec, compile, __import__, open).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax error: {e}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES:
                    return False, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in ALLOWED_MODULES:
                return False, f"Forbidden import: {node.module}"
            for alias in node.names:
                if alias.name == "*" or "__" in alias.name or alias.name in FORBIDDEN_CALLS:
                    return False, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                return False, f"Forbidden call: {node.func.id}"
            elif isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_CALLS:
                return False, f"Forbidden call: {node.func.attr}"

    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Name):
            names.append(node.id)
        if isinstance(node, ast.Attribute):
            names.append(node.attr)
        if isinstance(node, ast.alias):
            names.append(node.asname or "")
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.arg)):
            names.append(getattr(node, "name", getattr(node, "arg", "")))
        if any("__" in name for name in names):
            return False, "Forbidden dunder name or attribute"
        if isinstance(node, ast.ImportFrom) and node.level:
            return False, "Forbidden relative import"
    return True, None
