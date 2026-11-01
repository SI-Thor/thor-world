"""Tests for the why_stream module — the contract, the render, the mock,
and the door round (a fake CoreClient; no core, no sockets)."""

from __future__ import annotations

import json

import pytest

from world_mcp import why_stream
from world_mcp.why_stream import (
    ContractError,
    DoorNotWired,
    WhyDigest,
    fetch_digest,
    mock_digest_stream,
    why_door_events,
)


def _sample(extra: dict | None = None) -> dict:
    d = {
        "tick": 10391,
        "alive": 3206,
        "dead": 8,
        "unknown": 26,
        "absent": 0,
        "admitted_last": 2,
        "vetoed_last": 0,
        "skipped_infeasible_last": 0,
        "spent_gpu_seconds": 2952.30,
        "spent_joules": 73807.21,
        "spent_cents": 0.0,
        "cascade_pending": 0,
        "overrun_streak": 0,
        "safe_mode": False,
        "conservation_ok": True,
        "mass_total": 2173.90,
        "chain_hash": "2e8dcb4169e78ce1",
        "events_hashed": 16438,
        "events_dropped": 8246,
    }
    if extra:
        d.update(extra)
    return d


class TestContract:
    def test_parses_the_frozen_contract(self):
        d = WhyDigest.from_mapping(_sample())
        assert d.tick == 10391
        assert d.chain_hash == "2e8dcb4169e78ce1"
        assert d.safe_mode is False

    def test_json_roundtrip(self):
        d = WhyDigest.from_json(json.dumps(_sample()))
        assert d.alive == 3206

    def test_unknown_trailing_fields_are_tolerated_append_only(self):
        # The kernel may append fields; the parser must not break.
        d = WhyDigest.from_mapping(_sample({"gpu_hz": 100.0, "mig": "1g.5gb"}))
        assert d.extra == {"gpu_hz": 100.0, "mig": "1g.5gb"}

    def test_missing_required_field_raises(self):
        bad = _sample()
        del bad["chain_hash"]
        with pytest.raises(ContractError, match="chain_hash"):
            WhyDigest.from_mapping(bad)

    def test_ill_typed_field_raises(self):
        bad = _sample({"safe_mode": "yes please"})
        with pytest.raises(ContractError):
            WhyDigest.from_mapping(bad)


class TestRendering:
    def test_markdown_shape_is_stable(self):
        d = WhyDigest.from_mapping(_sample())
        md = d.render_markdown()
        assert md.startswith("# why — kernel digest")
        assert "purposes: 3206 alive / 26 unknown / 8 dead" in md
        assert "conservation: ok" in md
        # No narration — facts only (their law, kept).
        assert "because" not in md

    def test_markdown_flags_violations_loudly(self):
        d = WhyDigest.from_mapping(_sample({"conservation_ok": False}))
        assert "VIOLATED" in d.render_markdown()


class TestMockStream:
    def test_deterministic(self):
        a = list(mock_digest_stream(ticks=50, seed=7))
        b = list(mock_digest_stream(ticks=50, seed=7))
        assert a == b

    def test_every_mock_digest_parses(self):
        for raw in mock_digest_stream(ticks=100):
            WhyDigest.from_json(raw)  # raises if the mock drifts from contract

    def test_mock_models_harvest_not_deletion(self):
        # Population can wobble down, but nothing is ever removed from the
        # state totals: alive+unknown+dead+absent never decreases.
        total = None
        for raw in mock_digest_stream(ticks=400, start_alive=50):
            d = WhyDigest.from_json(raw)
            now = d.alive + d.unknown + d.dead + d.absent
            if total is not None:
                assert now >= total, "the mock must never delete purposes"
            total = now


def test_module_is_stdlib_only():
    # The purity law, mechanically: no builder-ide / core.* import sneaks in.
    import inspect

    src = inspect.getsource(why_stream)
    assert "import core" not in src and "from core" not in src


# ---------------------------------------------------------------------------
# The door round (research/ecology-why-door): reads through the core's HTTP
# API, never a side channel. A fake CoreClient stands in for the door.
# ---------------------------------------------------------------------------


class FakeDoor:
    """The smallest CoreClient: a GET that answers what it was given."""

    def __init__(self, payload=None, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.got: list[str] = []

    class _Refused(Exception):
        def __init__(self, status: int, message: str) -> None:
            super().__init__(message)
            self.status = status

    async def get(self, path: str, **_) -> dict:
        self.got.append(path)
        if self.status >= 400:
            raise self._Refused(self.status, f"core returned {self.status}")
        return self.payload


class FakeStream:
    """The smallest watch_events: replays a fixed event list, once."""

    def __init__(self, events: list[dict]) -> None:
        self.events = events
        self.types: list[list[str]] | None = None

    async def watch_events(self, types: list[str], **_):
        self.types = self.types or []
        self.types.append(list(types))
        for event in self.events:
            yield event


def test_reads_the_summary_door():
    import asyncio

    async def run():
        door = FakeDoor(payload={"digest": _sample()})
        digest = await fetch_digest(door)
        assert door.got == ["/v1/why/summary"]
        assert digest.tick == 10391
        assert digest.chain_hash == "2e8dcb4169e78ce1"

    asyncio.run(run())


def test_the_unwired_core_is_surfaced_not_faked():
    import asyncio

    async def run():
        door = FakeDoor(status=501)
        with pytest.raises(DoorNotWired, match="501"):
            await fetch_digest(door)

    asyncio.run(run())


def test_a_door_without_a_digest_is_a_contract_error():
    import asyncio

    async def run():
        door = FakeDoor(payload={"note": "half-wired"})
        with pytest.raises(ContractError):
            await fetch_digest(door)

    asyncio.run(run())


def test_the_stream_rides_the_existing_events_door():
    import asyncio

    async def collect():
        events = [
            {"seq": 10, "type": "why.digest", "payload": {"digest": _sample()}},
            {"seq": 11, "type": "why.digest",
             "payload": {"digest": _sample({"tick": 10392})}},
            # a tick that does not advance is filtered, not re-yielded
            {"seq": 12, "type": "why.digest",
             "payload": {"digest": _sample({"tick": 10392})}},
            # a bad frame is skipped, never fatal
            {"seq": 13, "type": "why.digest", "payload": {"digest": {}}},
        ]
        stream = FakeStream(events)
        return [d.tick async for d in why_door_events(stream)], stream

    ticks, stream = asyncio.run(collect())
    assert ticks == [10391, 10392]
    assert stream.types == [["why.digest"]]


def test_the_resource_degrades_when_the_door_is_not_wired():
    # the server's resource_why returns a notice naming the seam; the
    # module it imports stays out of the stdlib surface (purity law)
    import inspect

    src = inspect.getsource(why_stream)
    assert "import core" not in src and "from core" not in src
    assert "/v1/why/summary" in src  # the door, named in the module
