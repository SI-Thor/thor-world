"""Every resource, read through an MCP client at a live core.

The client here is the stdio subprocess — the way a local agent actually
runs the server — so these tests prove the whole stack: the package boots
in a clean process, finds the core from the environment, and composes
each resource from the core's own read doors.
"""

from __future__ import annotations

from conftest import resource_body


def test_the_seven_resources_are_listed(mcp):
    uris = sorted(str(r.uri) for r in mcp.list_resources().resources)
    assert uris == [
        "world://context",
        "world://dirty",
        "world://ledger",
        "world://recipe",
        "world://renders",
        "world://vocabulary",
        "world://why",
    ]


def test_the_recipe_versions_are_a_template_resource(mcp):
    tpls = mcp.list_resource_templates().resource_templates
    assert [t.uri_template for t in tpls] == \
        ["world://recipe/{version}{?against}"]


def test_the_context_packet_carries_the_situation(mcp):
    packet = resource_body(mcp.read("world://context"))
    # §1.4's halves: view, history, conversation, project
    assert "view" in packet and "history" in packet and "conversation" in packet
    project = packet["project"]
    assert project["branch"] == "br_main"
    assert project["head"].startswith("ver_")
    assert project["budget_left_tonight"]["gpu_minutes"] > 0
    assert project["vocabulary_gaps"] == ["bridge.rope"]
    assert project["vocabulary_words"] == 4
    assert "history" in packet and "work_orders_open" in packet["history"]
    # capability rows are the derived view over vocab.call counts
    assert any(row["word"] == "props.harbor.crates" for row in packet["capability_rows"])


def test_the_vocabulary_shows_the_edges(mcp):
    vocab = resource_body(mcp.read("world://vocabulary"))
    words = {w["word"]: w for w in vocab["words"]}
    assert set(words) == {"props.harbor.crates", "props.harbor.racks",
                          "material.wood.weathering", "bridge.rope"}
    assert words["bridge.rope"]["status"] == "coming-soon"
    # a word's params are a real schema with bounds — the door's contract
    crates = words["props.harbor.crates"]["params"]
    assert crates["additionalProperties"] is False
    assert crates["properties"]["count"]["minimum"] == 1


def test_the_recipe_resource_serves_the_diffable_history(mcp):
    recipe = resource_body(mcp.read("world://recipe"))
    branches = {b["id"]: b for b in recipe["branches"]}
    assert branches["br_main"]["purpose"] == "main"
    assert branches["br_main"]["head"].startswith("ver_")
    assert recipe["history"], "the root version's commit is in the log"
    root = recipe["history"][0]
    assert root["root_hash"].startswith("sha256:")
    assert root["author"] == "casey"
    # the two standing options (keep yesterday, back to the plan) ride with
    # every branch set — night-one furniture (X G7)
    assert [s["name"] for s in recipe["standing"]] == [
        "keep yesterday", "back to the plan"]


def test_a_recipe_version_reads_its_document(mcp):
    """The read door the core grew (GET /v1/recipe/{version}): the
    document itself, hash-verified at the core — an agent diffs recipes
    without a filesystem on the Thor."""
    recipe = resource_body(mcp.read("world://recipe"))
    head = next(b["head"] for b in recipe["branches"] if b["id"] == "br_main")
    row = next(h for h in recipe["history"] if h["version"] == head)
    ver = resource_body(mcp.read(f"world://recipe/{head}"))
    assert ver["version"] == head
    # the two doors agree on the hash — the history is the version record
    assert ver["root_hash"] == row["root_hash"]
    assert ver["actor"]["id"] == row["author"]
    # the document is the whole recipe, south span included (the host's seed)
    assert set(ver["document"]["entries"]) == {
        "harbor.crates", "harbor.racks", "wood.weathering",
        "structure.south_span"}


def test_a_recipe_version_diffs_against_its_base(mcp, core_server):
    """`?against=`: the canonical op list that turns the base into the
    version (§1.1: the diff IS the op list), base→version so a night's
    edits read forward."""
    url, _core = core_server
    recipe = resource_body(mcp.read("world://recipe"))
    root = recipe["history"][0]["version"]
    # someone edits the world through the core's own typed door
    import httpx
    httpx.post(url + "/v1/vocab/call", headers={
        "Authorization": "Bearer dev-token-propmaker"}, json={
            "word": "props.harbor.crates", "node": "crates_01",
            "params": {"count": 6, "style": "weathered", "from_library": 0,
                       "seed": 771}})
    head = next(b["head"] for b in
                resource_body(mcp.read("world://recipe"))["branches"]
                if b["id"] == "br_main")
    assert head != root
    diff = resource_body(
        mcp.read(f"world://recipe/{head}?against={root}"))
    assert diff["base"] == root and diff["version"] == head
    paths = {op["path"]: op["value"] for op in diff["ops"] if op["op"] == "set"}
    assert paths["harbor.crates.params.count"] == 6
    assert paths["harbor.crates.params.seed"] == 771


def test_an_unknown_version_is_refused_verbatim(mcp):
    """The read door's 404 rides the protocol error's message — the edge
    named, the way every refusal is surfaced here."""
    import pytest

    from mcp.shared.exceptions import MCPError

    with pytest.raises(MCPError, match="no version 'ver_nope'"):
        mcp.read("world://recipe/ver_nope")


def test_the_renders_resource_starts_honest(mcp):
    renders = resource_body(mcp.read("world://renders"))
    assert renders == {"renders": [], "critic_verdicts": []}


def test_the_ledger_carries_both_tallies(mcp):
    ledger = resource_body(mcp.read("world://ledger"))
    # the seeded history: the Prop-maker's earlier scored bets, with
    # denominators (never a lone score, X C2)
    tally = ledger["predictions"]["tally"]
    row = next(t for t in tally if t["builder"] == "prop-maker")
    assert row["denominator"] == 3
    assert row["line"].startswith("prop-maker: ")
    # taste rows are owner-authored and read-only here
    assert ledger["taste"]["count"] >= 1
    assert all(r["author"] == "casey" for r in ledger["taste"]["rows"])
    assert ledger["budget"]["rails"]["gpu_minutes"] == 360


def test_the_dirty_cursor_names_branch_and_head(mcp):
    dirty = resource_body(mcp.read("world://dirty"))
    cursor = dirty["cursor"]
    assert cursor["branch"] == "br_main"
    assert cursor["head"].startswith("ver_")
    # the cursor carries {branch, head} so a client fetches a delta, not
    # everything (RV patch 14)
    assert "seq" in cursor
