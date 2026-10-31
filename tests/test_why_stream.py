"""Tests for the draft why_stream module — stdlib only, purity-law clean."""

from __future__ import annotations

import json

import pytest

from world_mcp import why_stream
from world_mcp.why_stream import ContractError, WhyDigest, mock_digest_stream


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
