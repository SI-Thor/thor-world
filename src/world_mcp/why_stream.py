"""why_stream — the `world://why` resource, reading from a core door.

THE DOOR ROUND (research/ecology-why-door branch, 2026-11-01): the
review of the draft module was right — world-mcp is a *pure client* of
builder-ide's HTTP API (per-agent tokens, every action audited), so a
why-stream resource must read from a core door, not from the ecology's
own published JSON (a side channel the audit log cannot see). The door
this module reads is `GET /v1/why/summary`, proposed to builder-ide on
the research/ecology-bridge branch (`docs/design/why-door.md` there):

  * the ecology process publishes its tick digest through
    `POST /v1/why/digest` — one `why.digest` audit event per digest,
    written by the core's single writer like every other row;
  * `GET /v1/why/summary` folds the latest digest back out;
  * the existing SSE door (`GET /v1/events?types=why.digest`) is the
    live stream — no new stream door needed.

Until the core wires the door, an unwired core answers 501 with the
seam named (the `/v1/world/scene` precedent) and this module surfaces
that honestly (:class:`DoorNotWired`); the deterministic mock stays for
tests and pre-door builds.

The purity law holds: no builder-ide import, no server-side mutable
state; a pure transform from the door's JSON (the ecology's frozen,
append-only contract) to MCP resource content. The contract is
append-only: the parser requires the known fields below and tolerates
any number of unknown trailing fields — forward compatibility by
construction.
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


class DoorNotWired(RuntimeError):
    """The core does not serve the why door yet (501, the seam named).

    The `/v1/world/scene` precedent: an unwired core answers 501 with
    the seam's name in the body, and a client surfaces that rather than
    pretending. The resource degrades to a notice; the mock remains
    available for tests and pre-door builds.
    """

    def __init__(self, message: str = "the why door is not wired yet") -> None:
        super().__init__(message)


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

    The draws come from the same LCG the chain hash uses (Knuth's
    constants), seeded — deterministic without growing the module's
    import surface, which the purity test pins to stdlib-plus-httpx.
    """
    def draw(state: list[int]) -> float:
        """One LCG step, as a unit float: the top bits are the ones that
        move."""
        state[0] = (state[0] * 6364136223846793005 + 1442695040888963407) % (2**64)
        return state[0] / 2**64

    def below(state: list[int], chance: float) -> bool:
        return draw(state) < chance

    def between(state: list[int], high: int) -> int:
        return int(draw(state) * high)

    rng = [seed or 1]
    alive, unknown, dead = start_alive, 0, 0
    spent_gpu = 0.0
    chain = 0x2E8DCB4169E78CE1
    for tick in range(1, ticks + 1):
        # Population drift.
        if below(rng, 0.3):
            alive += 1
        if below(rng, 0.02):
            alive -= 1
            unknown += 1  # harvested: questionable, not gone
        if below(rng, 0.01) and unknown > 0:
            unknown -= 1
            alive += 1  # revived (on parole — the digest doesn't model that)
        if below(rng, 0.005):
            alive -= 1
            dead += 1
        admitted = between(rng, 4) if tick % 10 == 0 else 0
        vetoed = between(rng, 3) if tick % 10 == 0 else 0
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
            "skipped_infeasible_last": between(rng, 2) if tick % 10 == 0 else 0,
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


# ---------------------------------------------------------------------------
# The door round: reads through builder-ide's HTTP API (CoreClient), never
# through a side channel. TYPE_CHECKING only — the import stays out of the
# runtime path so the module remains importable where httpx is absent
# (the parser and the mock are stdlib-only; the door is world-mcp's edge).
# ---------------------------------------------------------------------------

async def fetch_digest(core) -> WhyDigest:
    """The latest WHY digest, read from the core's door.

    ``GET /v1/why/summary`` → :class:`WhyDigest`. A 501 raises
    :class:`DoorNotWired` (the unwired-core answer, the seam named);
    any other refusal propagates as the door's own error, verbatim —
    a refusal is information, not a fault.
    """
    try:
        payload = await core.get("/v1/why/summary")
    except Exception as exc:  # narrowed below; CoreError lives at the edge
        status = getattr(exc, "status", None)
        if status == 501:
            raise DoorNotWired(str(exc)) from exc
        raise
    digest = payload.get("digest") if isinstance(payload, dict) else None
    if digest is None:
        raise ContractError("the door answered without a digest")
    return WhyDigest.from_mapping(digest)


async def why_door_events(core, poll_types: tuple[str, ...] = ("why.digest",)):
    """Digests as the core's log grows — the door-shaped replacement for
    the draft's raw-kernel stream.

    Rides the existing SSE door (``watch_events(types=["why.digest"])``):
    one upstream connection, the core's own event fanout, every digest
    auditable by seq. Yields only when the tick actually advances
    (agents do not want a firehose; the door publishes, we filter).
    """
    last_tick: int | None = None
    async for event in core.watch_events(list(poll_types)):
        payload = event.get("payload") or {}
        digest_raw = payload.get("digest") or payload
        try:
            digest = WhyDigest.from_mapping(digest_raw)
        except ContractError:
            continue  # one bad frame is skipped, never fatal
        if digest.tick != last_tick:
            last_tick = digest.tick
            yield digest
