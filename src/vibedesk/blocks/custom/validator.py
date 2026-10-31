"""Safety and syntax validation for custom Blocks."""

from __future__ import annotations

import ast

FORBIDDEN_MODULES: set[str] = {
    "os",
    "sys",
    "subprocess",
    "shutil",
    "builtins",
    "importlib",
    "socket",
    "urllib",
    "requests",
    "httpx",
    "aiohttp",
    "http",
    "ftplib",
    "poplib",
    "imaplib",
    "smtplib",
    "telnetlib",
    "asyncio",
    "metatrader5",
    "mt5",
}

FORBIDDEN_CALLS: set[str] = {
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
                mod_root = alias.name.split(".")[0].lower()
                if mod_root in FORBIDDEN_MODULES:
                    return False, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mod_root = node.module.split(".")[0].lower()
                if mod_root in FORBIDDEN_MODULES:
                    return False, f"Forbidden import: {node.module}"
            for alias in node.names:
                if alias.name.lower() in FORBIDDEN_MODULES or alias.name in FORBIDDEN_CALLS:
                    return False, f"Forbidden import: {alias.name}"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
                return False, f"Forbidden call: {node.func.id}"
            elif isinstance(node.func, ast.Attribute) and node.func.attr in FORBIDDEN_CALLS:
                return False, f"Forbidden call: {node.func.attr}"

    return True, None
