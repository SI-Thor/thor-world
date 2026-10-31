"""The context packet, composed from the core's own read doors.

Core-spec §1.4: what the computer attaches to every in-game call so a few
spoken words become a full work order. The core-held half (project state,
budget, vocabulary and its gaps, recent history) is read here over HTTP;
the view half (selection, region, conversation labels) belongs to whoever
is looking at the world, so :func:`context_packet` accepts it and the
work-order tool passes it straight into ``/v1/call`` — the core captures
the authoritative packet itself.

The most recently captured packet (from the latest ``wo.filed`` event)
supplies the view and conversation when no selection is given, so the
resource shows the world as it was last seen, not an empty room.
"""

from __future__ import annotations

import anyio

from .client import CoreClient

#: The bounds the spec pins for the packet's history halves (§1.4.1); the
#: composition honours them even though the core would bound its own
#: capture too.
RECENT_CHANGES = 10
RECENT_RENDERS = 3
RECENT_CRITIC_NOTES = 5

OPEN_WO_STATES = ("draft", "queued", "building", "testing", "parked")

HISTORY_TYPES = ["wo.filed", "version.committed", "render.completed",
                 "critic.verdict"]


async def context_packet(core: CoreClient, *, view: dict | None = None,
                         conversation: list[dict] | None = None) -> dict:
    """Compose a §1.4-shaped packet from the core's doors."""
    slate, budget, vocab, branches, wos, events = await _gather(core)

    latest = next((e for e in reversed(events) if e.get("type") == "wo.filed"), None)
    snapshot = ((latest or {}).get("payload") or {}).get("packet_snapshot") or {}
    snap_view = snapshot.get("view") or {}
    snap_conv = snapshot.get("conversation") or []

    the_view = view or {
        "selection": snap_view.get("selection", []),
        "params": snap_view.get("params", {}),
        "region": snap_view.get("region"),
    }
    labels = conversation if conversation is not None else snap_conv

    main = _main_branch(branches)
    open_wos = [
        {"id": w["id"], "title": w["title"], "state": w["state"]}
        for w in wos.get("work_orders", []) if w.get("state") in OPEN_WO_STATES
    ]
    return {
        "actor": ((latest or {}).get("actor") or {}).get("id"),
        "view": the_view,
        "conversation": labels[-12:],
        "history": {
            "recipe_changes": [
                {"ver": e["payload"].get("version"), "actor": e["actor"]["id"],
                 "line": e["payload"].get("message", "")}
                for e in events if e.get("type") == "version.committed"
            ][-RECENT_CHANGES:],
            "renders": [
                {"at": e["at"], "artifact": (e.get("artifact") or {}).get("sha256"),
                 "payload": e["payload"]}
                for e in events if e.get("type") == "render.completed"
            ][-RECENT_RENDERS:],
            "critic_notes": [
                {"at": e["at"], "actor": e["actor"]["id"],
                 **{k: v for k, v in e["payload"].items() if k != "refusal"}}
                for e in events if e.get("type") == "critic.verdict"
            ][-RECENT_CRITIC_NOTES:],
            "work_orders_open": open_wos[:8],
        },
        "project": {
            "branch": main["id"] if main else None,
            "head": main.get("head") if main else None,
            "budget_left_tonight": budget.get("remaining") or budget.get("rails", {}),
            "bar": budget.get("bar"),
            "vocabulary_gaps": [
                w["word"] for w in vocab.get("words", [])
                if w.get("status") == "coming-soon"
            ][:8],
            "vocabulary_words": len(vocab.get("words", [])),
            "engine_pin": (snapshot.get("project") or {}).get("engine_pin", {}),
            "money": slate.get("money"),
        },
        "capability_rows": vocab.get("capability_rows", []),
    }


def _main_branch(branches: dict) -> dict | None:
    rows = branches.get("branches", [])
    for row in rows:
        if row.get("purpose") == "main":
            return row
    return rows[0] if rows else None


async def _gather(core: CoreClient):
    """Read the six doors at once; any failure fails the whole read loudly
    rather than half-composing a packet."""
    results: dict[str, object] = {}

    async def run(name: str, aw) -> None:
        results[name] = await aw

    async with anyio.create_task_group() as tg:
        tg.start_soon(run, "slate", core.get("/v1/slate"))
        tg.start_soon(run, "budget", core.get("/v1/budget"))
        tg.start_soon(run, "vocab", core.get("/v1/vocab"))
        tg.start_soon(run, "branches", core.get("/v1/branches"))
        tg.start_soon(run, "wos", core.get("/v1/work-orders"))
        tg.start_soon(run, "events", core.read_events(HISTORY_TYPES))
    return (results["slate"], results["budget"], results["vocab"],
            results["branches"], results["wos"], results["events"])
