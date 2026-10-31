"""The MCP server: resources, tools, and the ``world.dirty`` watcher.

Everything an agent can do here maps onto one door of the builder-ide core
(core-spec §4.2) — the server holds no state of its own beyond the event
cursor. The rules this server keeps, in the core's own spirit:

* **No tool runs arbitrary code in the world.** The only writes are the
  core's typed doors: a vocabulary call, a sealed prediction, a queued
  job, a filed work order. Refusals from the core are surfaced verbatim —
  the "I can't do rope yet" moment is the product, not an error to work
  around.
* **Everything is recorded with its author.** Each tool names, in its
  result, the actor the core's audit log recorded (the token's actor) and
  the agent that called it.
* **Sealed before rendered.** ``propose_recipe_edit`` seals the prediction
  first (§5.1); the render it predicts does not exist yet.
* **The world changes, the agent hears about it.** A single SSE
  connection per server process watches the core's log; every
  world-changing event publishes ``world://dirty`` (carrying ``{branch,
  head}``, RV patch 14) to all subscribed sessions.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import anyio

from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.server import MCPServer
from mcp.server.subscriptions import InMemorySubscriptionBus
from mcp.shared.subscriptions import ResourceUpdated

from . import __version__
from .client import CoreClient, CoreError
from .config import Settings
from .packet import context_packet

log = logging.getLogger(__name__)

#: Event types that make the *world* dirty: the recipe moved. Renders are
#: caches; they refresh ``world://renders`` but never dirty the world.
WORLD_DIRTY_TYPES = ["version.committed", "branch.head.moved", "branch.created",
                     "branch.rejected", "branch.proposed"]

#: Events that refresh the other resources without dirtying the world.
RENDER_TYPES = ["render.completed", "critic.verdict"]

CLAIM_TYPES = ["shadow_count", "scale_read", "clutter_density", "material_read",
               "era_read", "light_read", "weather_read", "taste_keep"]

CONFIDENCE = ["guess", "low", "medium", "high"]

INSTRUCTIONS = """You are working on a builder-ide world through its core — the
only doors are the typed vocabulary, the work order and the night's queue.

Read `world://context` first: it carries the selection, the recent history,
the project state, the budget left tonight, and the vocabulary and its gaps.

Rules of the house:
- You never write code into the world. A recipe edit is a typed vocabulary
  call (word + params validated against the word's schema), and anything
  the vocabulary cannot say is a work order, not a workaround.
- Seal your prediction before you ask for the render that tests it.
- A refusal from a door is honest information ("word is coming-soon",
  "param outside the schema"). Report it; do not retry around it.
- Every action you take is in the core's audit log under the actor whose
  token this server holds.
"""


class DirtyState:
    """The watcher's cursor — the only mutable state this server holds.

    Just the log position: the branch and head are derived from the dirty
    events themselves at every read (the watcher starts at the log's live
    edge, so events older than this process live only in the core's
    backlog — and the backlog is the truth anyway).
    """

    def __init__(self) -> None:
        self.last_seq: int = 0


def cursor_from_events(events: list[dict], *, branch: str | None = None,
                       seq: int = 0) -> dict:
    """Derive the ``{branch, head, at, seq}`` cursor from dirty events.

    Two payload dialects ride one log (builder-ide INTEGRATION §4): the
    recipe store spells a commit ``{"version": …}`` and a head move
    ``{"to": …}``; the spine's own seed commit spells ``{"head": …}``. The
    branch rides the event *subject* in both.
    """
    for event in events:
        seq = max(seq, int(event.get("seq", 0)))
        subject = event.get("subject") or {}
        if subject.get("entity_kind") == "branch" and subject.get("entity_id"):
            branch = subject["entity_id"]
    head = at = None
    for event in reversed(events):
        payload = event.get("payload") or {}
        head = payload.get("to") or payload.get("head") or payload.get("version")
        if head:
            at = event.get("at")
            break
    return {"branch": branch, "head": head, "at": at, "seq": seq}


def build_server(settings: Settings | None = None,
                 *, core: CoreClient | None = None,
                 bus: InMemorySubscriptionBus | None = None) -> MCPServer:
    settings = settings or Settings.from_env()
    core = core or CoreClient(settings)
    bus = bus or InMemorySubscriptionBus()
    dirty = DirtyState()

    @asynccontextmanager
    async def lifespan(server: MCPServer):
        # The watcher runs in the lifespan's own task group: closing the
        # lifespan cancels it, and with it the single upstream SSE
        # connection every subscriber is served from.
        async with anyio.create_task_group() as tg:
            tg.start_soon(_watch_world, core, bus, dirty)
            yield {"core": settings.core_url, "agent": settings.agent_name}
        await core.aclose()

    mcp = MCPServer(
        name="world-mcp",
        title="the builder-ide world",
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        subscriptions=bus,
    )
    _register_resources(mcp, core, dirty)
    _register_tools(mcp, core, settings, dirty)
    return mcp


async def _watch_world(core: CoreClient, bus: InMemorySubscriptionBus,
                       dirty: DirtyState) -> None:
    """One upstream SSE connection fans out to every subscribed session."""
    since = 0
    try:
        health = await core.health()
        since = int(health.get("seq", 0))
    except Exception as exc:  # noqa: BLE001 - watch from 0 on a blind start
        log.warning("healthz probe failed (%s); watching from seq 0", exc)
    async for event in core.watch_events(WORLD_DIRTY_TYPES + RENDER_TYPES,
                                         since_seq=since):
        try:
            dirty.last_seq = max(dirty.last_seq, int(event.get("seq", 0)))
            if event.get("type") in WORLD_DIRTY_TYPES:
                await bus.publish(ResourceUpdated(uri="world://dirty"))
                await bus.publish(ResourceUpdated(uri="world://context"))
                if event["type"] in ("version.committed", "branch.head.moved",
                                     "branch.created"):
                    await bus.publish(ResourceUpdated(uri="world://recipe"))
            else:
                await bus.publish(ResourceUpdated(uri="world://renders"))
        except Exception:  # noqa: BLE001 - one bad frame never kills the watch
            log.exception("world.dirty dispatch failed for %s", event.get("id"))


# ------------------------------------------------------------------ resources


def _register_resources(mcp: MCPServer, core: CoreClient, dirty: DirtyState) -> None:
    @mcp.resource(
        "world://context",
        name="the context packet",
        description="What the core attaches to every call (§1.4): the view, "
                    "recent history, the conversation labels, the project "
                    "state, the budget left tonight, and the vocabulary and "
                    "its gaps. Read this before acting.",
        mime_type="application/json",
    )
    async def resource_context() -> dict:
        return await context_packet(core)

    @mcp.resource(
        "world://vocabulary",
        name="the vocabulary",
        description="Every word (including coming-soon edges) with its typed "
                    "param schema, the taste notes (read-only) and the "
                    "capability rows. A word's schema is the contract a "
                    "vocabulary call must satisfy.",
        mime_type="application/json",
    )
    async def resource_vocabulary() -> dict:
        return await core.get("/v1/vocab")

    @mcp.resource(
        "world://recipe",
        name="the recipe and its history",
        description="Branch heads and the committed version history (hashes, "
                    "messages, authors). The recipe is the source of truth; "
                    "meshes and renders are caches.",
        mime_type="application/json",
    )
    async def resource_recipe() -> dict:
        branches = await core.get("/v1/branches")
        events = await core.read_events(["version.committed"])
        versions = [
            {"version": e["payload"].get("version"),
             "parent": e["payload"].get("parent"),
             "root_hash": e["payload"].get("root_hash"),
             "message": e["payload"].get("message"),
             "author": e["actor"]["id"], "at": e["at"]}
            for e in events
        ]
        return {
            "branches": branches.get("branches", []),
            "standing": branches.get("standing", []),
            "history": versions,
            "note": "the recipe document itself is content-addressed in the "
                    "core's blob store; no HTTP door serves it yet (the "
                    "seam /v1/world/scene answers 501). The history and the "
                    "hashes are the diffable truth a night's work leaves.",
        }

    @mcp.resource(
        "world://renders",
        name="the latest renders and critic verdicts",
        description="Recent render.completed artifacts and critic verdicts, "
                    "newest last. A verdict is a word, never a colour.",
        mime_type="application/json",
    )
    async def resource_renders() -> dict:
        events = await core.read_events(RENDER_TYPES)
        return {
            "renders": [e for e in events if e["type"] == "render.completed"],
            "critic_verdicts": [e for e in events if e["type"] == "critic.verdict"],
        }

    @mcp.resource(
        "world://ledger",
        name="the ledger tallies",
        description="The two tallies kept apart (§1.7): predictions about the "
                    "world (scored by the critic) and taste notes (written by "
                    "the owner only), plus the budget rails.",
        mime_type="application/json",
    )
    async def resource_ledger() -> dict:
        return {
            "predictions": await core.get("/v1/predictions/tally"),
            "taste": await core.get("/v1/ledger/taste", principal="owner"),
            "budget": await core.get("/v1/budget"),
        }

    @mcp.resource(
        "world://dirty",
        name="the world.dirty cursor",
        description="What changed in the world and when: the cursor carries "
                    "{branch, head, seq} so a client fetches a since_version "
                    "delta, not everything (RV patch 14). Subscribe to this "
                    "resource to be notified when the world changes.",
        mime_type="application/json",
    )
    async def resource_dirty() -> dict:
        events = await core.read_events(WORLD_DIRTY_TYPES,
                                        since_seq=max(0, dirty.last_seq - 12))
        return {
            "cursor": cursor_from_events(events, seq=dirty.last_seq),
            "recent": events[-12:],
        }


# ------------------------------------------------------------------ tools


def _register_tools(mcp: MCPServer, core: CoreClient, settings: Settings,
                    dirty: DirtyState) -> None:
    def author_note(recorded: Any) -> dict:
        return {
            "recorded_as": recorded,
            "agent": settings.agent_name,
        }

    async def main_branch() -> dict | None:
        branches = await core.get("/v1/branches")
        for row in branches.get("branches", []):
            if row.get("purpose") == "main":
                return row
        return None

    # ---------------------------------------------------------- reads

    @mcp.tool(
        name="get_context",
        description="Read the context packet for a view (§1.4): the selection's "
                    "resolved referents and current params, the recent history, "
                    "the conversation labels, the project state, the budget "
                    "left tonight, and the vocabulary and its gaps. Pass the "
                    "node ids you are looking at; omit them for the world's "
                    "own situation.",
    )
    async def tool_get_context(
        selection: list[str] | None = None,
        region: str | None = None,
        params: dict[str, dict] | None = None,
        conversation: list[dict] | None = None,
    ) -> dict:
        view: dict = {}
        if selection is not None:
            view["selection"] = selection
        if region is not None:
            view["region"] = region
        if params is not None:
            view["params"] = params
        return await context_packet(core, view=view or None,
                                    conversation=conversation)

    @mcp.tool(
        name="list_vocabulary",
        description="The closed, typed vocabulary: words with their kinds, "
                    "owners, statuses, param schemas and taste notes. Filter "
                    "by status to see the coming-soon edges — the gaps that "
                    "only a work order can close.",
    )
    async def tool_list_vocabulary(status: str | None = None) -> dict:
        vocab = await core.get("/v1/vocab")
        words = vocab.get("words", [])
        if status:
            words = [w for w in words if w.get("status") == status]
        vocab["words"] = words
        vocab.update(author_note("core vocab projection (read-only)"))
        return vocab

    @mcp.tool(
        name="read_night_plan",
        description="The night's plan and its price: the slated tasks with "
                    "their asks (the cost model's p50, or the filer's "
                    "override), the bar (the night's rails), the owner's two "
                    "lines — what tonight costs and whether it fits — and the "
                    "budget left if a night is running.",
    )
    async def tool_read_night_plan() -> dict:
        out: dict[str, Any] = {
            "slate": await core.get("/v1/slate"),
            "budget": await core.get("/v1/budget"),
        }
        out.update(author_note("the slate is read-only here; starting or "
                               "stopping the night is the owner's door"))
        return out

    # ---------------------------------------------------------- the sealed-bet loop

    @mcp.tool(
        name="propose_recipe_edit",
        description="Change the world through the typed vocabulary, sealing "
                    "the prediction FIRST (§5.1: the seal is committed before "
                    "the render exists). The word's param schema is the "
                    "contract; anything outside it is refused at the door and "
                    "the refusal is honest. The render that tests the "
                    "prediction is requested separately (request_render), so "
                    "the bet stays sealed before its test exists.",
    )
    async def tool_propose_recipe_edit(
        word: str,
        params: dict[str, Any],
        prediction: dict[str, Any],
        node: str | None = None,
        region: str | None = None,
    ) -> dict:
        # prediction: {claim, claim_type, confidence, basis?, rung, resolution, seed}
        claim_type = prediction.get("claim_type")
        if claim_type and claim_type not in CLAIM_TYPES:
            raise ToolError(
                f"claim_type must be one of the closed set {CLAIM_TYPES} (§1.7)")
        confidence = prediction.get("confidence", "guess")
        if confidence not in CONFIDENCE:
            raise ToolError(f"confidence must be one of {CONFIDENCE}")
        render_spec = {k: prediction[k] for k in ("rung", "resolution", "seed")
                       if k in prediction}
        branch = await main_branch()
        branch_ref = f"{(branch or {}).get('id', 'br_main')}@{(branch or {}).get('head', 'head')}"
        try:
            sealed = await core.post(
                "/v1/predictions/seal",
                {"branch_ref": branch_ref, "claim": prediction["claim"],
                 "claim_type": claim_type or "material_read",
                 "confidence": confidence,
                 "basis": prediction.get("basis", "0 observations"),
                 "render_spec": render_spec},
                principal="character")
            call = await core.post(
                "/v1/vocab/call",
                {"word": word, "params": params, "node": node, "region": region},
                principal="character")
        except CoreError as exc:
            raise ToolError(_refusal(exc)) from exc
        result = {
            "sealed_prediction": sealed.get("prediction"),
            "edit": call.get("edit"),
            "committed": call.get("committed", False),
            "version": call.get("version"),
            **author_note((call.get("edit") or {}).get("author", {}).get("id")),
        }
        return result

    @mcp.tool(
        name="request_render",
        description="Request a render or preview of entries at a rung of the "
                    "ladder (1 fast layout · 2 mid with lighting and "
                    "materials · 3 full). The request is queued at the "
                    "core's door priced by the measured cost model (kind "
                    "rung.N); the engine that renders it is the recipe half's "
                    "seat, so until it lands the queue row is the honest "
                    "state. Price a sealed prediction against the SAME rung "
                    "and seed you render.",
    )
    async def tool_request_render(
        entry_paths: list[str],
        rung: int,
        seed: int | None = None,
        branch: str | None = None,
    ) -> dict:
        if rung not in (1, 2, 3):
            raise ToolError("rung must be 1, 2 or 3 (the preview ladder, §2.3)")
        intention = (f"render rung {rung} of {', '.join(entry_paths)}"
                     + (f" @ seed {seed}" if seed is not None else "")
                     + (f" on {branch}" if branch else ""))
        try:
            task = await core.post(
                "/v1/tasks",
                {"intention": intention, "kind": f"rung.{rung}"},
                principal="character")
        except CoreError as exc:
            raise ToolError(_refusal(exc)) from exc
        row = task.get("task", {})
        return {
            "task": row,
            "price": {"ask": row.get("cost_estimate"), "rail": row.get("rail"),
                      "ask_source": row.get("ask_source"),
                      "basis": row.get("ask_basis")},
            "render_spec": {"rung": rung, "seed": seed,
                            "entry_paths": entry_paths},
            "note": "queued on the slate/night lane; the engine seat renders it",
            **author_note(row.get("author")),
        }

    # ---------------------------------------------------------- work orders

    @mcp.tool(
        name="file_work_order",
        description="File a work order from a terse request plus the context "
                    "packet: the core captures the packet, resolves the "
                    "referents through the node registry, and compiles either "
                    "a draft (with the one-line confirmation and BOTH price "
                    "lines) or — when two referent sets are both plausible — a "
                    "one-tap question. It never guesses; answer the question "
                    "with answer_question. Fill intent.spec_sentence (≤ 25 "
                    "words), intent.goal, intent.acceptance (≥ 1 machine-"
                    "checkable check) and intent.estimate {build_verify, "
                    "generate_test} to shape the compile.",
    )
    async def tool_file_work_order(
        request: str,
        selection: list[str] | None = None,
        region: str | None = None,
        params: dict[str, dict] | None = None,
        conversation: list[dict] | None = None,
        intent: dict[str, Any] | None = None,
    ) -> dict:
        body: dict[str, Any] = {"utterance": request, "intent": intent or {}}
        view: dict[str, Any] = {}
        if selection is not None:
            view["selection"] = selection
        if region is not None:
            view["region"] = region
        if params is not None:
            view["params"] = params
        if view:
            body["view"] = view
        if conversation is not None:
            body["conversation"] = conversation
        try:
            result = await core.post("/v1/call", body, principal="owner")
        except CoreError as exc:
            raise ToolError(_refusal(exc)) from exc
        filed = ((result.get("draft") or {}).get("filed_by") or {}).get("id")
        result.update(author_note(filed))
        result["confirm_line"] = (result.get("draft") or {}).get("confirm_line")
        return result

    @mcp.tool(
        name="confirm_work_order",
        description="Answer a draft work order's confirmation: go (queue it), "
                    "no (cancel), or edit (cancel and refile with amendments). "
                    "This is the owner's one word; the call is idempotent by "
                    "key. Pass the idem_key again if a retry must not "
                    "double-file.",
    )
    async def tool_confirm_work_order(
        work_order: str,
        answer: str,
        amendments: dict[str, Any] | None = None,
        idem_key: str | None = None,
    ) -> dict:
        if answer not in ("go", "no", "edit"):
            raise ToolError("answer must be go | no | edit")
        try:
            # the core answers with the work order record itself, in its new
            # state (queued | cancelled | refiled)
            record = await core.post(
                f"/v1/work-orders/{work_order}/confirm",
                {"answer": answer, "amendments": amendments},
                principal="owner", idem_key=idem_key)
        except CoreError as exc:
            raise ToolError(_refusal(exc)) from exc
        filed_by = record.get("filed_by") if isinstance(record, dict) else None
        who = filed_by.get("id") if isinstance(filed_by, dict) else None
        return {"work_order": record, **author_note(who)}

    @mcp.tool(
        name="answer_question",
        description="Answer a parked question (the never-guess rule: when the "
                    "compile found two plausible referent sets it asked, and "
                    "the answer unblocks the work order). The options are on "
                    "the question; answer with one of them, in the owner's "
                    "words.",
    )
    async def tool_answer_question(question: str, answer: str) -> dict:
        try:
            result = await core.post(
                f"/v1/questions/{question}/answer", {"answer": answer},
                principal="owner")
        except CoreError as exc:
            raise ToolError(_refusal(exc)) from exc
        # the question record carries the answer, not who gave it; the log
        # has the author (the actor behind the owner token)
        who = "owner"
        try:
            events = await core.read_events(["question.answered"])
            ev = next((e for e in reversed(events)
                       if (e.get("subject") or {}).get("entity_id") == question),
                      None)
            if ev:
                who = (ev.get("actor") or {}).get("id") or who
        except CoreError:
            pass  # the author note degrades; the answer itself stands
        return {**result, **author_note(who)}

    # ---------------------------------------------------------- the night

    @mcp.tool(
        name="queue_genbatch",
        description="Queue a generative batch (genbatch): a night of props "
                    "made by the local image model + TRELLIS.2, measured, "
                    "judged and recorded as assets with provenance and "
                    "licence. The kind prices it from the measured cost model "
                    "— prop.512 or prop.1024 per variant; count scales the "
                    "ask (the p50) and the rail (the p95). The requests "
                    "themselves ride the intention (what to make, how many "
                    "seeds); the genbatch seat consumes the queue.",
    )
    async def tool_queue_genbatch(
        intention: str,
        kind: str = "prop.512",
        count: int = 1,
        night: str | None = None,
        cost_estimate: dict[str, float] | None = None,
    ) -> dict:
        body: dict[str, Any] = {"intention": intention, "kind": kind}
        if night:
            body["night"] = night
        if cost_estimate:
            body["cost_estimate"] = cost_estimate
        try:
            task = await core.post("/v1/tasks", body, principal="character")
        except CoreError as exc:
            raise ToolError(_refusal(exc)) from exc
        # the count scales the ask: re-file with the model's own arithmetic
        row = task.get("task", {})
        if count > 1:
            try:
                task = await core.post(
                    "/v1/tasks",
                    {"intention": f"{intention} ×{count}", "kind": kind,
                     "cost_estimate": _scaled(row.get("cost_estimate"), count)},
                    principal="character")
                row = task.get("task", {})
            except CoreError as exc:
                raise ToolError(_refusal(exc)) from exc
        return {
            "task": row,
            "price": {"ask": row.get("cost_estimate"), "rail": row.get("rail"),
                      "ask_source": row.get("ask_source"),
                      "basis": row.get("ask_basis"),
                      "marked": "[e]" if row.get("ask_basis") == "estimate" else ""},
            **author_note(row.get("author")),
        }

    @mcp.tool(
        name="subscribe_world_dirty",
        description="Subscribe to world.dirty: what changed in the world and "
                    "when. Returns the cursor {branch, head, seq} and the "
                    "dirty events since `since_seq` (rewind is free — the "
                    "feed is the log). Clients that hold a subscription "
                    "stream receive world://dirty resource-updated "
                    "notifications the moment the recipe moves; this tool is "
                    "the polling form for clients that cannot.",
    )
    async def tool_subscribe_world_dirty(since_seq: int = 0) -> dict:
        events = await core.read_events(WORLD_DIRTY_TYPES, since_seq=since_seq)
        return {
            "cursor": cursor_from_events(events, seq=dirty.last_seq),
            "events": events,
            "push": "subscribe to the world://dirty resource; the server "
                    "publishes an update the moment the core's log moves",
            **author_note("a read; the core records no actor for it"),
        }


def _scaled(rail: dict | None, count: int) -> dict:
    rail = dict(rail or {})
    for key in ("gpu_minutes", "wall_minutes"):
        if key in rail:
            rail[key] = round(float(rail[key]) * count, 3)
    if "hosted_cents" in rail:
        rail["hosted_cents"] = int(rail["hosted_cents"]) * count
    return rail


def _refusal(exc: CoreError) -> str:
    """A core refusal, verbatim — the edges are the story."""
    extra = " ".join(f"[{k}={v}]" for k, v in exc.extra.items())
    return f"{exc.message}{' ' + extra if extra else ''} (core: {exc.code})"
