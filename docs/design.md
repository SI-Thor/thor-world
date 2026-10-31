# world-mcp — the design note

*What this server is, where it sits, and why it is built the way it is.
The brief it serves is `BRIEF.md` in the repo root; the world it serves is
builder-ide (`docs/design/core-spec.md` there, cited below by section).*

---

## 1. Where the server sits: a client of the core's own doors

world-mcp is a **pure client of `core.api`'s HTTP API** — JSON over the
endpoints of core-spec §4.2, plus the SSE log of §4.3. It never imports
`core.*`, never opens the ledger, never writes a row. A test
(`tests/test_purity.py`) walks the package's imports and fails if a
builder-ide name ever appears, so the seam cannot drift silently.

The alternative — importing `core.svc` and calling the services in-process
— was rejected, on four grounds, all of them the core's own rules:

1. **One writer owns the ledger (§1.0).** The services assume they sit on
   the single writer's connection; a second process importing them and
   touching the same SQLite file is exactly the corruption §1.0 exists to
   prevent. Over HTTP, every write queues through the one writer and
   inherits its idempotency and locking for free.
2. **The doors are the product.** Authorization (§4.1's token classes),
   audit (`aud_` events with the actor), refusals ("refused at the door"),
   idempotency keys — all live at the HTTP layer. A client of the doors
   gets them all; an importer of internals would have to re-implement each
   and would drift.
3. **Deployment.** The core runs on the Thor; the MCP server runs wherever
   its agent runs — a local agent's stdio subprocess on the ProArt, or a
   hosted process on the Thor for remote agents. Same server code, same
   doors, no shared interpreter or venv required.
4. **Version skew is honest over HTTP.** When the core grows a door, an
   old server fails loudly at that door (a 404 becomes a named refusal)
   instead of silently calling a stale Python symbol.

The one module that *does* import builder-ide is `demo/core_host.py` — the
harness side of the seam. It opens a core the way builder-ide's own tests
do (the `RecipeStoreSeam` over the one ledger, opt-in per INTEGRATION §6),
seeds the Fjellvik world, and serves it with uvicorn. The tests and the
demo run against it; the server package never sees it.

## 2. The mapping: every tool is one door, every resource is a read

Nothing in this server invents a capability. Each tool calls exactly one
endpoint with exactly one token class; each resource composes reads.

| MCP surface | Core door (§4.2) | Principal | Notes |
|---|---|---|---|
| resource `world://context` | `/v1/slate` + `/v1/budget` + `/v1/vocab` + `/v1/branches` + `/v1/work-orders` + `/v1/events` | panel | the §1.4 packet, composed; see §3 below |
| resource `world://vocabulary` | `/v1/vocab` | panel | words incl. coming-soon edges, taste notes read-only |
| resource `world://recipe` | `/v1/branches` + `version.committed` events | panel | hashes + history; the versions themselves read at the template below |
| resource `world://recipe/{version}{?against}` | `GET /v1/recipe/{version}` (+ `?against=` diff) | panel | the document, hash-verified at the core; `?against=<base>` gives the op list base→version (§1.1) |
| resource `world://renders` | `render.completed` + `critic.verdict` events | panel | newest last; a verdict is a word |
| resource `world://ledger` | `/v1/predictions/tally` + `/v1/ledger/taste` (owner read) + `/v1/budget` | panel + owner | the two tallies kept apart (§1.7) |
| resource `world://dirty` | `/v1/events` backlog | panel | the cursor, §5 below |
| tool `get_context` | the same six reads, with the caller's view | panel | the view half belongs to whoever is looking |
| tool `list_vocabulary` | `/v1/vocab` | panel | `status` filter shows the gaps |
| tool `read_night_plan` | `/v1/slate` + `/v1/budget` | panel | the plan and its price; starting the night is *not* offered |
| tool `propose_recipe_edit` | `/v1/predictions/seal` then `/v1/vocab/call` | character | seal first (§5.1); refusals verbatim |
| tool `request_render` | `/v1/tasks` (kind `rung.N`) | character | priced by the cost model; the engine seat renders it |
| tool `propose_plan_intention` | `/v1/tasks` (kind `plan.intention`) | character | a proposal on the slate; the band is the proposal's own footprint (marked [e]) |
| tool `file_work_order` | `/v1/call` | owner | utterance + view → question XOR draft + confirm line |
| tool `confirm_work_order` | `/v1/work-orders/{id}/confirm` | owner | go / no / edit; idempotent by key |
| tool `answer_question` | `/v1/questions/{id}/answer` | owner | the never-guess loop closes |
| tool `queue_genbatch` | `/v1/tasks` (kind `prop.*`) | character | count scales the ask; unknown kinds refused |
| tool `subscribe_world_dirty` | `/v1/events` backlog | panel | the polling form of the push path |

**Authorship.** The actor the core records is the token's actor (§4.1:
a token is an identity). The server holds three token classes — owner
(`/v1/call` and confirms), character (vocab calls, seals, slate work),
panel (reads and the stream) — and the owner and character tokens are
**per-agent tokens**: their `agent` claim lands on every audit row they
write, `{kind, id, via, agent}`, so the log itself answers "which agent
acted for whom" from the row alone. Each tool reports the author the log
recorded — the actor and the claim on it — and the server holds no agent
name of its own to stamp.
`tests/test_tools.py::test_the_author_is_on_every_write` checks the log,
not the tool's say-so.

**What is deliberately absent.** No tool runs arbitrary code in the world;
the only writes are the typed doors listed above. Also absent on purpose:
writing the taste ledger or the vocabulary (the agent never holds the pen,
§2.5), starting or stopping the night, applying a branch (§4.2 gates those
to the owner at the core; an agent that needs them files an order and the
owner confirms). A coming-soon word, a param outside a schema, an unknown
genbatch kind — each is refused by the core and surfaced **verbatim** as
the tool's error text. The refusal is information, not an exception to
work around.

## 3. The context packet: composed for reading, captured for acting

Core-spec §1.4 gives the packet two halves. The **view half** (selection,
region, params, conversation labels) belongs to whoever is looking at the
world, so `get_context` and `file_work_order` accept it from the caller and
`/v1/call` performs the authoritative capture — stored as an event, so any
later "why did it read 'these' as the south span?" is answerable. The
**core-held half** (project state, budget left tonight, vocabulary gaps,
recent history) the server composes from the six read doors, concurrently.
When no view is supplied, the packet falls back to the view captured by the
latest `wo.filed` — the world as it was last seen, not an empty room.

## 4. Transports

* **stdio** — `world-mcp serve --stdio` (the default). A local agent's
  subprocess; nothing but protocol frames ever reaches stdout.
* **streamable HTTP** — `world-mcp serve --http --host 0.0.0.0 --port
  8808`, path `/mcp`. The ProArt's agents reach a Thor-hosted server over
  the LAN. `world-mcp check` probes the core once so a misconfigured URL
  fails in a second, not inside a client session.

Push notifications (`world://dirty` to subscribers) use the MCP
`subscriptions/listen` stream, which requires the 2026-07-28 protocol
era; the SDK's high-level client reaches it with its default `auto`
mode (probe `server/discover`). Handshake-era clients get everything
except the push: the polling form is the `subscribe_world_dirty` tool.

## 5. `world.dirty`: one upstream stream, every subscriber served

The server holds **one** SSE connection to the core's `/v1/events`
(§4.3: `since_seq` makes reconnects and rewind the same operation). A
watcher task — started in the server's lifespan, so its lifetime is the
server's — classifies each event:

* recipe events (`version.committed`, `branch.*`) publish
  `world://dirty`, `world://context` and `world://recipe` updates;
* render/critic events refresh `world://renders` without dirtying the
  world (renders are caches).

The MCP SDK's subscription bus fans each notification out to every
subscribed session. The cursor itself — `{branch, head, at, seq}` — is
**derived from the log at every read**, not kept in server memory: the
watcher starts at the live edge, so events older than this process exist
only in the backlog, and the backlog is the truth anyway (rewind is
free). Two payload dialects ride one log (builder-ide INTEGRATION §4 —
the recipe store spells commits `{"version": …}` and head moves
`{"to": …}`, the spine spells `{"head": …}`); the derivation reads both,
with the branch taken from the event subject. A client that sees the
cursor fetches a `since_version` delta, not everything (RV patch 14).

## 6. How tiles plug in: cloud authors write through these same doors

The vision ("What the Thor is for"): **no LLMs on the Thor**. Cloud
subscriptions are the *authors* — they write scripts; those scripts
become **tiles**, the options the Thor's local decision models choose
between in the moment; every play's outcome is recorded, and the
decision models learn from the wins and the losses.

The tiles branch (in progress on builder-ide) will need no new kind of
client. A tile author is just another agent at this server, and the
loop already exists:

1. **Read the situation** — `world://context`, `world://vocabulary` (a
   tile that plays against a gap is aimed at a real demand: the packet
   names the gaps).
2. **Write the tile** — a tile is a script plus its claim, which is a
   work order's shape: `file_work_order` with the script as the
   deliverable and the acceptance check the tile must pass; the owner
   confirms; the night builds and gates it; a keep promotes it.
3. **Record the outcome** — the core's audit log already records every
   play with its author; tile outcomes are just more ledger events, so
   the learning loop's data lands where the night's bets already land.

When the tiles design lands its own doors, they should be **two**, and
this server will mirror them one-for-one, like every other surface:

* a **write door** for tile scripts — content-addressed artifacts with
  provenance and licence (the assets door, §1.6, already has the shape),
  exposed as a `world://tiles` resource and a `submit_tile` tool;
* an **outcome event** (`tile.played`) appended by whatever plays the
  tile, exposed through `world://ledger` and the dirty stream.

What must *not* change: the server still never runs a tile — the Thor
plays tiles, the cloud authors them, this server files and reads.
Authorship, sealed bets to test a tile's claim before it is trusted,
refusals verbatim: the invariants are the server, not the surface.

## 7. Testing the seam

`pytest` boots, per test, a real seeded core behind a real uvicorn
server on a temp `$BUILDER_HOME`, then drives the world-mcp server the
way agents really run it — as a stdio subprocess — through the official
MCP client. The streamable-HTTP transport gets its own fixture and the
push test. The suite refuses in-memory shortcuts: the claims are about
sockets, subprocesses, audit logs and refusals, and those are facts
about the real stack. `demo/agent_session.py` is the same walk, printed:
a scripted agent session (no real LLM) from terse request to queued
order to the ledger naming its author.

## 8. Open questions for Casey

1. **Agent identity at the core.** Today the server's own name rides in
   tool results beside the actor, because §4.1's token classes carry an
   actor but not an agent. Per-agent tokens (an `agent` claim inside the
   token, one actor, many agents) would move that into the audit log
   where it belongs.
2. **The recipe document has no read door.** `world://recipe` serves
   branches, hashes and history; the document itself is a
   content-addressed blob (and `/v1/world/scene` answers 501 until the
   compiler lands). A `GET /v1/recipe/{version}` door would let an agent
   diff recipes without a filesystem on the Thor.
3. **Render requests have no engine behind them yet** (the rungs are
   scripted stand-ins engine-side), so `request_render` honestly returns
   the queue row, not a preview. When the warm scene server lands
   (§1.9), the same tool should return the artifact ref too — the tool's
   shape is ready for it.
4. **Budget writes.** An agent can read the night's plan and price but
   cannot shape the slate (chips ride `/v1/tasks` for the Companion
   character). If cloud authors should file *plan* intentions and not
   only genbatches, `/v1/tasks` needs a kind for it — a one-line core
   change, mirrored here as one tool.
5. **Concurrent reads on the store.** Under load we drew a rare 500 from
   the core's doors — `sqlite3.InterfaceError` in `core.store`'s
   `count_events` — when two store-reading GETs landed in the threadpool
   in the same tick (the store serves endpoints from FastAPI's threads
   over one SQLite connection). This server now takes the doors strictly
   one at a time, so it never races the store; the ProArt client and any
   other concurrent caller still could. A lock (or a per-thread
   connection) around the store's reads would close it for everyone.
