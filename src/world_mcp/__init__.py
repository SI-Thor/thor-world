"""world-mcp — the MCP server over the builder-ide world.

Any agent (Claude Code, zcode, mcode, other cloud LLMs) works on a
builder-ide world **only through the core's own doors**: the HTTP API of
``core.api``. This package is a pure client of that API — it never imports
builder-ide, never writes a ledger, never runs code in the world. Every
action lands in the core's audit log under the token's actor, so everything
an agent does is recorded with its author.

The pieces:

* :mod:`world_mcp.config` — the environment (core URL, the three token
  classes, the agent's name).
* :mod:`world_mcp.client` — the HTTP client: JSON doors plus a reader for
  the SSE event stream (rewind is free; the feed is the log).
* :mod:`world_mcp.packet` — the context packet, composed from the core's
  own read doors (core-spec §1.4).
* :mod:`world_mcp.server` — the MCP server: resources, tools, and the
  ``world.dirty`` watcher that pushes resource-updated notifications.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
