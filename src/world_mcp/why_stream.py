"""why_stream — a draft `world://why` resource for world-mcp.

PUBLICATION-SURFACE MODULE, DRAFT FOR REVIEW (research/ecology-why-stream
branch). Not wired into server.py — that integration belongs to the
thor-world team. This module respects the purity law: stdlib only, no
builder-ide imports, no server-side mutable state; it is a pure transform
from the ecology-thor kernel's frozen JSON contract to MCP resource
content.

The contract (ecology-thor docs/INTEGRATION.md) is append-only: new
fields are appended, never reordered or removed. This parser therefore
(a) requires the known fields below, and (b) tolerates any number of
unknown trailing fields — forward compatibility by construction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import AsyncIterator, Dict, Iterator, Mapping, Optional

# The frozen contract, as of ecology-thor v0.1.0. Do not reorder; only
# append (see the proposal doc).
_REQUIRED_FIELDS = (
    "tick",
    "alive",
    "dead",
    "unknown",
    "absent",
    "admitted_last",
    "vetoed_last",
    "skipped_infeasible_last",
    "spent_gpu_seconds",
    "spent_joules",
    "spent_cents",
    "cascade_pending",
    "overrun_streak",
    "safe_mode",
    "conservation_ok",
    "mass_total",
    "chain_hash",
    "events_hashed",
    "events_dropped",
)


class ContractError(ValueError):
    """The digest violated the frozen contract (missing/ill-typed field)."""


@dataclass(frozen=True)
class WhyDigest:
    """One kernel tick summary — numbers and states, no narration."""

    tick: int
    alive: int
    dead: int
    unknown: int
    absent: int
    admitted_last: int
    vetoed_last: int
    skipped_infeasible_last: int
    spent_gpu_seconds: float
    spent_joules: float
    spent_cents: float
    cascade_pending: int
    overrun_streak: int
    safe_mode: bool
    conservation_ok: bool
    mass_total: float
    chain_hash: str
    events_hashed: int
    events_dropped: int
    # Unknown trailing fields ride along (append-only contract).
    extra: Dict[str, object] = None  # type: ignore[assignment]

    @classmethod
    def from_json(cls, raw: str) -> "WhyDigest":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - trivial
            raise ContractError(f"not JSON: {exc}") from exc
        return cls.from_mapping(data)

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> "WhyDigest":
        missing = [f for f in _REQUIRED_FIELDS if f not in data]
        if missing:
            raise ContractError(f"missing required field(s): {', '.join(missing)}")
        # Bools must BE bools — Python truthiness would silently accept
        # strings/numbers and change the meaning of safe_mode/conservation.
        for bool_field in ("safe_mode", "conservation_ok"):
            if not isinstance(data[bool_field], bool):
                raise ContractError(f"{bool_field} must be a JSON boolean")
        try:
            digest = cls(
                tick=int(data["tick"]),
                alive=int(data["alive"]),
                dead=int(data["dead"]),
                unknown=int(data["unknown"]),
                absent=int(data["absent"]),
                admitted_last=int(data["admitted_last"]),
                vetoed_last=int(data["vetoed_last"]),
                skipped_infeasible_last=int(data["skipped_infeasible_last"]),
                spent_gpu_seconds=float(data["spent_gpu_seconds"]),
                spent_joules=float(data["spent_joules"]),
                spent_cents=float(data["spent_cents"]),
                cascade_pending=int(data["cascade_pending"]),
                overrun_streak=int(data["overrun_streak"]),
                safe_mode=bool(data["safe_mode"]),
                conservation_ok=bool(data["conservation_ok"]),
                mass_total=float(data["mass_total"]),
                chain_hash=str(data["chain_hash"]),
                events_hashed=int(data["events_hashed"]),
                events_dropped=int(data["events_dropped"]),
                extra={k: v for k, v in data.items() if k not in _REQUIRED_FIELDS},
            )
        except (TypeError, ValueError) as exc:
            raise ContractError(f"ill-typed field: {exc}") from exc
        return digest

    def render_markdown(self) -> str:
        """The `world://why` resource body. Facts only, fixed order —
        the shape is part of the contract (append, never reorder)."""
        lines = [
            "# why — kernel digest",
            "",
            f"- tick: {self.tick}",
            f"- purposes: {self.alive} alive / {self.unknown} unknown / {self.dead} dead / {self.absent} absent",
            f"- last auction: {self.admitted_last} admitted, {self.vetoed_last} vetoed, {self.skipped_infeasible_last} infeasible",
            f"- spent: {self.spent_gpu_seconds:.1f} gpu-s, {self.spent_joules:.0f} J, {self.spent_cents:.2f} c",
            f"- cascade pending: {self.cascade_pending}",
            f"- overrun streak: {self.overrun_streak}",
            f"- safe mode: {str(self.safe_mode).lower()}",
            f"- conservation: {'ok' if self.conservation_ok else 'VIOLATED'}",
            f"- mass total: {self.mass_total:.1f}",
            f"- chain: {self.chain_hash} over {self.events_hashed} events"
            + (f", {self.events_dropped} unretained" if self.events_dropped else ""),
        ]
        if self.extra:
            lines.append(f"- (kernel newer than parser: {len(self.extra)} extra field(s))")
        return "\n".join(lines) + "\n"


async def why_events(source: AsyncIterator[str]) -> AsyncIterator[WhyDigest]:
    """Digests as they change — shaped like the `_watch_world` loop:
    yield only when the tick actually advances (agents don't want a
    100 Hz firehose; the kernel publishes, we filter)."""
    last_tick: Optional[int] = None
    async for raw in source:
        digest = WhyDigest.from_json(raw)
        if digest.tick != last_tick:
            last_tick = digest.tick
            yield digest


def mock_digest_stream(
    ticks: int = 100, start_alive: int = 120, seed: int = 42
) -> Iterator[str]:
    """Deterministic mock digests — the buildable target for IDE/viewer
    work before any door exists. Same shape as the kernel's snapshot_json.

    A tiny world: population wobbles, occasional stalls harvest a purpose
    to unknown, occasional revives, one auction every 10 ticks.
    """
    import random

    rng = random.Random(seed)
    alive, unknown, dead = start_alive, 0, 0
    spent_gpu = 0.0
    chain = 0x2E8DCB4169E78CE1
    for tick in range(1, ticks + 1):
        # Population drift.
        if rng.random() < 0.3:
            alive += 1
        if rng.random() < 0.02:
            alive -= 1
            unknown += 1  # harvested: questionable, not gone
        if rng.random() < 0.01 and unknown > 0:
            unknown -= 1
            alive += 1  # revived (on parole — the digest doesn't model that)
        if rng.random() < 0.005:
            alive -= 1
            dead += 1
        admitted = rng.randrange(0, 4) if tick % 10 == 0 else 0
        vetoed = rng.randrange(0, 3) if tick % 10 == 0 else 0
        spent_gpu += admitted * 0.8
        chain = (chain * 6364136223846793005 + 1442695040888963407) % (2**64)
        digest = {
            "tick": tick,
            "alive": max(alive, 0),
            "dead": dead,
            "unknown": unknown,
            "absent": 0,
            "admitted_last": admitted,
            "vetoed_last": vetoed,
            "skipped_infeasible_last": rng.randrange(0, 2) if tick % 10 == 0 else 0,
            "spent_gpu_seconds": round(spent_gpu, 3),
            "spent_joules": round(spent_gpu * 25.0, 2),
            "spent_cents": 0.0,
            "cascade_pending": 0,
            "overrun_streak": 0,
            "safe_mode": False,
            "conservation_ok": True,
            "mass_total": round(max(alive, 0) * 0.9 + unknown * 0.9, 3),
            "chain_hash": f"{chain:016x}",
            "events_hashed": tick * 3,
            "events_dropped": 0,
        }
        yield json.dumps(digest)
