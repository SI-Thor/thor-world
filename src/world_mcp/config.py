"""The environment world-mcp runs against.

Three token classes, one per door the core opens to this server
(core-spec §4.1 — a token is an identity, not a permission):

* the **owner** token calls ``/v1/call`` (the terse-request door that
  compiles a work order) and confirms it;
* the **character** token makes typed vocabulary calls, seals predictions,
  and queues work on the slate;
* the **panel** token is read-only — every resource read and the event
  stream.

The actor behind each token is the author the core's audit log records.
The defaults are the development tokens of a fresh ``$BUILDER_HOME``
(`core.config.default_tokens`); a real deployment issues per-agent tokens
and names them here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_CORE_URL = "http://127.0.0.1:8000"


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


@dataclass(frozen=True)
class Settings:
    #: Where the core's HTTP API listens (``uvicorn core.api:app``).
    core_url: str
    #: Owner token — /v1/call, confirm, answer (the owner's doors).
    owner_token: str
    #: Character token — /v1/vocab/call, /v1/predictions/seal, /v1/tasks.
    character_token: str
    #: Panel token — reads and the SSE stream; never writes.
    panel_token: str
    #: The agent's own name, shown in every tool result beside the actor
    #: the core recorded (the audit author is the token's actor; the agent
    #: identity rides here because the core has no agent token class yet).
    agent_name: str
    #: How long a one-shot SSE backlog read may take, seconds.
    sse_read_timeout: float = 2.5
    #: Stop an SSE backlog read after this many quiet seconds past data.
    sse_quiet_seconds: float = 0.35

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            core_url=_env("BUILDER_CORE_URL", DEFAULT_CORE_URL).rstrip("/"),
            owner_token=_env("WORLD_MCP_OWNER_TOKEN", "dev-token-casey"),
            character_token=_env("WORLD_MCP_CHARACTER_TOKEN", "dev-token-propmaker"),
            panel_token=_env("WORLD_MCP_PANEL_TOKEN", "dev-token-panel"),
            agent_name=_env("WORLD_MCP_ACTOR", "world-mcp-agent"),
            sse_read_timeout=float(_env("WORLD_MCP_SSE_TIMEOUT", "2.5")),
            sse_quiet_seconds=float(_env("WORLD_MCP_SSE_QUIET", "0.35")),
        )

    def token(self, principal: str) -> str:
        if principal == "owner":
            return self.owner_token
        if principal == "character":
            return self.character_token
        if principal == "panel":
            return self.panel_token
        raise ValueError(f"unknown principal {principal!r} (owner|character|panel)")
