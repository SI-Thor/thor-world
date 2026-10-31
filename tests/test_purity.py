"""The server is a pure client of the core's HTTP API — pinned in code.

world_mcp must never import builder-ide (``core.*``, ``builder_usd.*``): the
core is the single writer that owns the ledger, and a second writer process
would break core-spec §1.0. The wired core host lives in ``demo/`` and is
test/demo machinery only. This test walks the package's imports and fails
on any builder-ide name, so the seam cannot drift silently.
"""

from __future__ import annotations

import ast
from pathlib import Path

import world_mcp

PACKAGE = Path(world_mcp.__file__).parent
FORBIDDEN_ROOTS = ("core", "builder_usd", "builder_ide")


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_the_server_never_imports_builder_ide():
    for path in sorted(PACKAGE.glob("*.py")):
        roots = _imports_of(path)
        bad = roots.intersection(FORBIDDEN_ROOTS)
        assert not bad, (
            f"{path.name} imports {sorted(bad)} — the server is a client of "
            "the core's HTTP API, never of its internals (core-spec §1.0: "
            "one writer owns the ledger)")


def test_the_server_depends_only_on_mcp_and_http():
    allowed = {"world_mcp", "mcp", "mcp_types", "httpx", "anyio", "json",
               "logging", "asyncio", "contextlib", "dataclasses", "os",
               "typing", "uuid", "argparse", "sys", "__future__", "pathlib"}
    for path in sorted(PACKAGE.glob("*.py")):
        assert _imports_of(path) <= allowed, (
            f"{path.name} grew a dependency outside the server's surface "
            f"(allowed: {sorted(allowed)})")
