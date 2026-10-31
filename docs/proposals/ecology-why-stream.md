# Proposal: `world://why` — a WHY-ledger digest resource for world-mcp

*From the ecology-thor team (SI-Thor/ecology-thor, private for now).
Draft for the thor-world builders. Nothing here is merged intent; the
module on this branch is a proposal artifact that respects every law we
found in your README, docs/design.md and the 38-test suite — especially
the purity law.*

## The idea, in one paragraph

ecology-thor (the kernel builder-ide's fit study deferred — see
`docs/research/cogeco.md` there, and our follow-up on builder-ide's
`research/ecology-bridge` branch) now runs the WHY ledger + attention
auction as a native process on the Thor. world-mcp agents should be able
to *see* it: which purposes are alive, which are questionable, what the
last auction admitted, whether the box is in safe mode — the same
"why is the world doing this" answer your ledger gives for edits, but
for the box's attention itself. Your architecture already has the
perfect shape for publishing that: a resource (`world://why`) beside
`world://dirty`, fed through the existing `_watch_world` SSE fan-out,
with zero server-side state and zero builder-ide imports.

## Why it fits your laws

| Your law | How this respects it |
|---|---|
| Purity (`test_purity.py`: never import `core.*`) | `why_stream.py` imports nothing but the stdlib. The digest arrives as JSON — via a door when one exists, via the mock generator until then. |
| Server holds no mutable world state | The module is a pure transform: JSON digest in → MCP resource content out. The watcher pattern (`_watch_world`) stays exactly yours. |
| Authorship is read from the log, never stamped | The digest carries the kernel's own chain hash and event counters — it *is* a log read, published by the kernel that wrote it. |
| Refusals verbatim, no narration | The digest is numbers and states, no prose. Rendering choices stay in the client. |
| One tool = one door (future `submit_tile` pattern) | When the core grows `/v1/why/*` doors, `CoreClient` gains one method per door — the draft shows the exact signature. |

## The frozen contract (the part to review hardest)

The kernel's summary JSON (`docs/INTEGRATION.md` in ecology-thor) is
**append-only fields, never reordered**:

```json
{
  "tick": 10391,
  "alive": 3206, "dead": 8, "unknown": 26, "absent": 0,
  "admitted_last": 2, "vetoed_last": 0, "skipped_infeasible_last": 0,
  "spent_gpu_seconds": 2952.30, "spent_joules": 73807.21, "spent_cents": 0.0,
  "cascade_pending": 0, "overrun_streak": 0, "safe_mode": false,
  "conservation_ok": true, "mass_total": 2173.90,
  "chain_hash": "2e8dcb4169e78ce1", "events_hashed": 16438, "events_dropped": 8246
}
```

`why_stream.py` parses exactly this shape (tolerant of *new* trailing
fields — that's the append-only rule working) and renders the MCP
resource. If the contract needs a field you'd want in the IDE (e.g. a
per-purpose breakdown for a `world://why/{id}` resource), say so now —
adding later is append-only and safe, but designing the first ten
together is better.

## What's on this branch

- `src/world_mcp/why_stream.py` — the draft module:
  - `WhyDigest.from_json()` — strict parse of the frozen contract;
  - `WhyDigest.render_markdown()` — the `world://why` resource body
    (numbers + states only, no narration, per your taste);
  - `why_events(digest_source)` — an async iterator shaped like your
    SSE watcher loop: yields digests when they change (tick advances);
  - `mock_digest_stream()` — deterministic mock digests so the viewer
    and IDE can be built against the contract **before any door
    exists** (this is the thing we'd most like you to have: a
    buildable target today).
- `tests/test_why_stream.py` — pure-stdlib tests: contract parse
  tolerance (unknown trailing fields OK, missing required fields raise),
  markdown rendering stability, mock stream determinism.

Nothing is wired into `server.py` — that's your call, your PR. The
module + tests are drop-in when you want them.

## The door we'd ask builder-ide for (eventually)

`GET /v1/why/summary` → the JSON above (the kernel publishes it via a
seqlock; a door wrapper is a read, not a write — no new writer, no new
event type, closed-set safe). Until then the mock stream exists, and the
kernel can also expose the JSON over a localhost port that a
`_watch_world`-shaped poller reads — whichever your review prefers.

## Questions for your review

1. Resource shape: one `world://why` digest, or digest + per-purpose
   details (`world://why/{id}`) from the start?
2. Update cadence: ride the existing dirty-event SSE (kernel ticks at
   100 Hz are too chatty — we'd publish on auction boundaries, 10 Hz,
   or on state changes only)?
3. Should the mock stream land in `demo/` instead, so it never smells
   like production code to your purity tests?

— the ecology-thor team
