"""Dynamic loader for AI-authored custom Blocks in blocks/custom/ (spec §8, ADR 0012)."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from vibedesk.blocks.custom.registry import CustomBlockMeta, CustomBlockRegistry
from vibedesk.blocks.custom.validator import validate_custom_block_code
from vibedesk.blocks.registry import BlockDef, Kind

FRAMEWORK_FILES: set[str] = {
    "__init__.py",
    "registry.py",
    "validator.py",
    "loader.py",
    "lookahead.py",
}


def custom_block(
    name: str,
    kind: Kind | str,
    params: type[BaseModel],
    doc: str = "",
) -> Callable:
    """Decorator to declare a custom block definition on a function."""

    def decorator(fn: Callable) -> Callable:
        k = Kind(kind) if isinstance(kind, str) else kind
        d = doc or (fn.__doc__ or "").strip()
        bdef = BlockDef(name=name, kind=k, params=params, fn=fn, doc=d)
        fn._block_def = bdef  # type: ignore[attr-defined]
        return fn

    return decorator


class CustomBlockLoader:
    """Discovers, validates, and dynamically registers custom blocks from disk."""

    def __init__(
        self,
        directory: Path | str | None = None,
        registry: CustomBlockRegistry | None = None,
    ) -> None:
        self.directory = Path(directory) if directory is not None else Path(__file__).parent
        self.registry = registry if registry is not None else CustomBlockRegistry()

    def load_file(self, path: Path | str) -> CustomBlockMeta:
        """Load a single custom block python file."""
        file_path = Path(path)
        code = file_path.read_text(encoding="utf-8")
        sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()

        # Step 1: Safety and syntax validation
        ok, err = validate_custom_block_code(code)
        if not ok:
            meta = CustomBlockMeta(
                name=file_path.stem,
                kind="unknown",
                code=code,
                approved=False,
                approved_at=None,
                sha256=sha256,
                error=err,
            )
            self.registry._blocks[file_path.stem] = meta
            return meta

        # Step 2: Dynamic import
        module_name = f"vibedesk.blocks.custom.{file_path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            meta = CustomBlockMeta(
                name=file_path.stem,
                kind="unknown",
                code=code,
                approved=False,
                approved_at=None,
                sha256=sha256,
                error="Failed to create importlib module spec",
            )
            self.registry._blocks[file_path.stem] = meta
            return meta

        import sys

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            meta = CustomBlockMeta(
                name=file_path.stem,
                kind="unknown",
                code=code,
                approved=False,
                approved_at=None,
                sha256=sha256,
                error=f"Execution error: {e}",
            )
            self.registry._blocks[file_path.stem] = meta
            return meta

        # Step 3: Extract BlockDef
        bdef = self._extract_block_def(mod, file_path.stem)
        if bdef is None:
            meta = CustomBlockMeta(
                name=file_path.stem,
                kind="unknown",
                code=code,
                approved=False,
                approved_at=None,
                sha256=sha256,
                error="No valid BlockDef found in module",
            )
            self.registry._blocks[file_path.stem] = meta
            return meta

        # Step 4: Register or update in registry
        if bdef.name in self.registry._blocks:
            meta = self.registry.update_code(bdef.name, code=code, block_def=bdef)
        else:
            meta = self.registry.register(
                name=bdef.name,
                kind=bdef.kind.value,
                code=code,
                block_def=bdef,
            )

        return meta

    def _extract_block_def(self, mod: Any, stem: str) -> BlockDef | None:
        """Find BlockDef from module attributes or conventions."""
        if hasattr(mod, "BLOCK_DEF") and isinstance(mod.BLOCK_DEF, BlockDef):
            return mod.BLOCK_DEF

        if hasattr(mod, "BLOCK_NAME") and hasattr(mod, "BLOCK_KIND"):
            name = str(mod.BLOCK_NAME)
            kind = Kind(mod.BLOCK_KIND)
            params = getattr(
                mod, "Params", getattr(mod, f"{name.title().replace('_', '')}Params", None)
            )
            fn = getattr(mod, name, getattr(mod, "run", getattr(mod, "fn", None)))
            if params is not None and callable(fn):
                return BlockDef(
                    name=name,
                    kind=kind,
                    params=params,
                    fn=fn,
                    doc=(fn.__doc__ or "").strip(),
                )

        # Look for decorated function
        for _, obj in inspect.getmembers(mod):
            if hasattr(obj, "_block_def") and isinstance(obj._block_def, BlockDef):
                return obj._block_def

        # Convention: function named `stem` and class `Params`
        fn = getattr(mod, stem, None)
        params = getattr(mod, "Params", None)
        kind_attr = getattr(mod, "KIND", None)
        if callable(fn) and params is not None and kind_attr is not None:
            return BlockDef(
                name=stem,
                kind=Kind(kind_attr),
                params=params,
                fn=fn,
                doc=(fn.__doc__ or "").strip(),
            )

        return None

    def load_all(self) -> list[CustomBlockMeta]:
        """Discover and load all custom block files in the directory."""
        results: list[CustomBlockMeta] = []
        if not self.directory.exists():
            return results

        for file_path in sorted(self.directory.glob("*.py")):
            filename = file_path.name
            if (
                filename.startswith("_")
                or filename.startswith(".")
                or filename in FRAMEWORK_FILES
                or filename.startswith("test_")
                or filename.endswith("_test.py")
            ):
                continue

            meta = self.load_file(file_path)
            results.append(meta)

        return results


def load_custom_blocks(
    directory: Path | str | None = None,
    registry: CustomBlockRegistry | None = None,
) -> list[CustomBlockMeta]:
    """Convenience function to discover and load all custom blocks."""
    loader = CustomBlockLoader(directory=directory, registry=registry)
    return loader.load_all()
