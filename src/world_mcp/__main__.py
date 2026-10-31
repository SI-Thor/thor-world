"""``world-mcp`` — the command line.

Transport (the brief asks for both):

* ``world-mcp`` / ``world-mcp serve --stdio`` — stdio, for local agents
  (Claude Code, zcode, mcode as CLI peers);
* ``world-mcp serve --http --host 0.0.0.0 --port 8808`` — streamable
  HTTP at ``/mcp``, so the ProArt's agents reach a Thor-hosted server
  over the LAN.

Environment: see :mod:`world_mcp.config` — ``BUILDER_CORE_URL`` and the
three token classes.

``world-mcp check`` probes the core's ``/healthz`` once and reports, so a
misconfigured URL fails in one second instead of inside a client session.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from . import __version__


def _logging_to_stderr(verbose: bool) -> None:
    # stdio IS the protocol: nothing but frames may ever reach stdout.
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        stream=sys.stderr,
        format="%(name)s %(levelname)s %(message)s",
    )
    # httpx logs every request at INFO; the watcher's long-lived stream
    # would narrate itself forever
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _check() -> int:
    from .client import CoreClient
    from .config import Settings

    settings = Settings.from_env()
    core = CoreClient(settings)
    try:
        health = await core.health()
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        print(f"core at {settings.core_url}: unreachable ({exc})", file=sys.stderr)
        return 1
    finally:
        await core.aclose()
    print(f"core at {settings.core_url}: ok (seq {health.get('seq')}, "
          f"fold_seq {health.get('fold_seq')})")
    return 0


async def _serve(args: argparse.Namespace) -> int:
    from .config import Settings
    from .server import build_server

    server = build_server(Settings.from_env())
    if args.transport == "stdio":
        await server.run_stdio_async()
        return 0
    await server.run_streamable_http_async(
        host=args.host, port=args.port, streamable_http_path=args.path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="world-mcp",
        description="the MCP server over the builder-ide world")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check", help="probe the core once and report")

    serve = sub.add_parser("serve", help="serve the world over MCP")
    serve.add_argument("--stdio", dest="transport", action="store_const",
                       const="stdio", default="stdio",
                       help="stdio transport (default; for local agents)")
    serve.add_argument("--http", dest="transport", action="store_const",
                       const="http",
                       help="streamable HTTP (for agents reaching the Thor)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8808)
    serve.add_argument("--path", default="/mcp")
    serve.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args(argv)
    _logging_to_stderr(getattr(args, "verbose", False))
    if args.command == "check":
        return asyncio.run(_check())
    if args.command == "serve" or args.command is None:
        return asyncio.run(_serve(args))
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
