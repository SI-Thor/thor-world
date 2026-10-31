"""Every tool, called through an MCP client at a live core.

The client is the stdio subprocess (a clean process, the way agents run
the server). The core object rides along from the fixture so tests can
read the ledger directly and assert what the *audit log* recorded — the
brief's "everything an agent does is recorded with its author" is checked
at the log, not at the tool's say-so.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import tool_body

OWNER = {"Authorization": "Bearer dev-token-casey"}
PROP = {"Authorization": "Bearer dev-token-propmaker"}

STURDIER_INTENT = {
    "title": "Sturdier south span",
    "goal": "Raise the south span load factor to 2.0, keeping the sway",
    "spec_sentence": "reinforce the south span so the analytic load check passes at 2x",
    "acceptance": [{"kind": "constraint", "spec": {
        "path": "structure.south_span.params.load_factor", "op": ">=",
        "value": 2.0, "method": "analytic_member_forces"}}],
    "estimate": {"build_verify": {"gpu_minutes": 20, "wall_minutes": 40},
                 "generate_test": {"gpu_minutes": 10, "wall_minutes": 20}},
}


def _events(core, types):
    return core.store.events_since(0, types=types)


# ------------------------------------------------------------- the reads


def test_get_context_with_a_selection(mcp):
    body = tool_body(mcp.call("get_context", {"selection": ["span_south"]}))
    assert body["view"]["selection"] == ["span_south"]
    assert body["project"]["vocabulary_gaps"] == ["bridge.rope"]


def test_list_vocabulary_filter_status(mcp):
    body = tool_body(mcp.call("list_vocabulary", {"status": "coming-soon"}))
    assert [w["word"] for w in body["words"]] == ["bridge.rope"]


def test_read_night_plan_prices_the_plan(mcp):
    body = tool_body(mcp.call("read_night_plan"))
    slate = body["slate"]
    assert slate["bar"]["gpu_minutes"] == 360  # the model's default night rail
    assert "local" in slate["money"]  # hosted spend is off by default
    assert "Work orders" in slate["work_order_line"]


# ------------------------------------------------------- the sealed-bet loop


def test_propose_recipe_edit_seals_first_then_edits(mcp, core_server):
    _url, core = core_server
    result = tool_body(mcp.call("propose_recipe_edit", {
        "word": "props.harbor.crates", "node": "crates_01",
        "params": {"count": 6, "style": "weathered", "from_library": 0,
                   "seed": 771},
        "prediction": {
            "claim": "six weathered crates will read as weathered timber",
            "claim_type": "material_read", "confidence": "guess",
            "basis": "0 observations",
            "rung": 2, "resolution": 1024, "seed": 771}}))
    # the sealed prediction carries its render spec (X R7) and a seal hash
    sealed = result["sealed_prediction"]
    assert sealed["seal_hash"].startswith("sha256:")
    assert sealed["render_spec"]["rung"] == 2
    assert sealed["namespace"] == "world"
    # the edit committed through the real store
    assert result["committed"] is True
    assert result["version"].startswith("ver_")
    assert result["recorded_as"] == "prop-maker"
    # the agent claim comes from the core's row, not the server's say-so
    assert result["agent"] == "world-mcp"

    # §5.1 at the log: the seal precedes the call that precedes the commit
    seqs = {e.type: e.seq for e in _events(
        core, ["prediction.sealed", "vocab.call", "version.committed"])}
    assert seqs["prediction.sealed"] < seqs["vocab.call"] < seqs["version.committed"]
    assert _events(core, ["vocab.call"])[-1].actor.id == "prop-maker"
    assert _events(core, ["vocab.call"])[-1].actor.agent == "world-mcp"


def test_a_coming_soon_word_is_refused_at_the_door(mcp):
    result = mcp.call("propose_recipe_edit", {
        "word": "bridge.rope", "node": "span_south", "params": {"planks": 8},
        "prediction": {"claim": "a rope bridge", "claim_type": "material_read",
                       "confidence": "guess", "rung": 1, "resolution": 512,
                       "seed": 1}})
    assert result.is_error
    text = result.content[0].text
    assert "coming-soon" in text and "refuse" in text.lower()


def test_params_outside_the_schema_are_refused(mcp):
    result = mcp.call("propose_recipe_edit", {
        "word": "props.harbor.crates", "node": "crates_01",
        "params": {"count": 6, "colour": "blue"},  # colour is not in the word
        "prediction": {"claim": "blue crates", "claim_type": "material_read",
                       "confidence": "guess", "rung": 1, "resolution": 512,
                       "seed": 2}})
    assert result.is_error
    assert "refused at the door" in result.content[0].text


def test_request_render_is_priced_from_the_cost_model(mcp):
    body = tool_body(mcp.call("request_render", {
        "entry_paths": ["harbor.crates"], "rung": 2, "seed": 42}))
    assert body["task"]["kind"] == "rung.2"
    assert body["price"]["ask"]["gpu_minutes"] > 0
    assert body["render_spec"] == {"rung": 2, "seed": 42,
                                   "entry_paths": ["harbor.crates"]}
    assert body["recorded_as"] == "prop-maker"
    assert body["agent"] == "world-mcp"  # read from the task's audit row


def test_request_render_rejects_a_rung_off_the_ladder(mcp):
    result = mcp.call("request_render",
                      {"entry_paths": ["harbor.crates"], "rung": 4})
    assert result.is_error
    assert "rung must be 1, 2 or 3" in result.content[0].text


# ------------------------------------------------------------- work orders


def test_file_work_order_compiles_the_terse_request(mcp, core_server):
    _url, core = core_server
    result = tool_body(mcp.call("file_work_order", {
        "request": "make these sturdier",
        "selection": ["span_south"],
        "intent": STURDIER_INTENT}))
    assert "draft" in result and "question" not in result
    draft = result["draft"]
    assert draft["targets"] == ["span_south"]  # resolved through the registry
    # the one-line confirmation carries BOTH price lines (a one-line
    # estimate is a build failure, G §1 20:38)
    line = draft["confirm_line"]
    assert line.startswith("CONFIRM:")
    assert "build/verify:" in line and "generate/test:" in line
    assert line.endswith("Go?")
    assert result["confirm_line"] == line
    assert result["recorded_as"] == "casey"
    assert result["agent"] == "world-mcp"  # the row answers, not the server
    # the packet the core captured is in the log, with the author
    filed = _events(core, ["wo.filed"])[-1]
    snapshot = filed.payload.get("packet_snapshot") or {}
    assert snapshot["view"]["selection"] == ["span_south"]


def _file_ambiguous(mcp):
    """The spec's two-referent test (D §21:08): ambiguity asks, never guesses."""
    result = tool_body(mcp.call("file_work_order", {
        "request": "weather these",
        "selection": ["span_south", "crates_01"],  # two entry-path groups
        "intent": {"spec_sentence": "weather the selected timber",
                   "estimate": STURDIER_INTENT["estimate"]}}))
    assert "question" in result and "draft" not in result
    assert len(result["question"]["options"]) == 2
    return result


def test_two_plausible_referents_return_a_question_not_a_plan(mcp):
    _file_ambiguous(mcp)


def test_the_question_loop_closes(mcp):
    result = _file_ambiguous(mcp)
    q_id = result["question"]["id"]
    choice = result["question"]["options"][0]
    answered = tool_body(mcp.call("answer_question",
                                  {"question": q_id, "answer": choice}))
    # the Question record carries the answer itself (answered_at rides too;
    # there is no `open` flag on the record — the projection keeps that)
    assert answered["question"]["answer"] == choice
    assert answered["recorded_as"] == "casey"
    assert answered["agent"] == "world-mcp"
    # refiling with the resolved selection compiles the draft
    again = tool_body(mcp.call("file_work_order", {
        "request": "weather these",
        "selection": ["span_south"],
        "intent": {"spec_sentence": "weather the selected timber",
                   "estimate": STURDIER_INTENT["estimate"]}}))
    assert "draft" in again


def test_confirm_work_order_go_queues_it(mcp, core_server):
    _url, core = core_server
    draft = tool_body(mcp.call("file_work_order", {
        "request": "make these sturdier", "selection": ["span_south"],
        "intent": STURDIER_INTENT}))["draft"]
    confirmed = tool_body(mcp.call("confirm_work_order",
                                   {"work_order": draft["id"], "answer": "go"}))
    assert confirmed["work_order"]["state"] == "queued"
    assert confirmed["recorded_as"] == "casey"
    assert confirmed["agent"] == "world-mcp"
    row = _events(core, ["wo.confirmed"])[-1].actor
    assert (row.id, row.agent) == ("casey", "world-mcp")


def test_confirm_work_order_no_cancels(mcp):
    draft = tool_body(mcp.call("file_work_order", {
        "request": "make these sturdier", "selection": ["span_south"],
        "intent": STURDIER_INTENT}))["draft"]
    body = tool_body(mcp.call("confirm_work_order",
                              {"work_order": draft["id"], "answer": "no"}))
    assert body["work_order"]["state"] == "cancelled"


def test_confirm_is_idempotent_by_key(mcp):
    draft = tool_body(mcp.call("file_work_order", {
        "request": "make these sturdier", "selection": ["span_south"],
        "intent": STURDIER_INTENT}))["draft"]
    args = {"work_order": draft["id"], "answer": "go", "idem_key": "go-once"}
    first = tool_body(mcp.call("confirm_work_order", args))
    second = tool_body(mcp.call("confirm_work_order", args))
    assert first["work_order"]["id"] == second["work_order"]["id"]
    assert second["work_order"]["state"] == "queued"


# ------------------------------------------------------------- the night


def test_queue_genbatch_prices_the_batch(mcp, core_server):
    _url, core = core_server
    body = tool_body(mcp.call("queue_genbatch", {
        "intention": "rope-coil props for the south span, 2 seeds each",
        "kind": "prop.512", "count": 6}))
    assert body["task"]["kind"] == "prop.512"
    assert body["price"]["ask_source"] == "override"  # the count scales it
    single = 0.26791666666666664
    assert body["price"]["ask"]["gpu_minutes"] == pytest.approx(single * 6, rel=1e-3)
    assert body["recorded_as"] == "prop-maker"
    assert body["agent"] == "world-mcp"
    assert _events(core, ["night.task.created"])[-1].payload["task"]["kind"] \
        == "prop.512"


def test_queue_genbatch_refuses_an_unknown_kind(mcp):
    result = mcp.call("queue_genbatch",
                      {"intention": "something", "kind": "prop.4096"})
    assert result.is_error
    assert "cost model has no price" in result.content[0].text


def test_subscribe_world_dirty_reads_the_backlog(mcp):
    body = tool_body(mcp.call("subscribe_world_dirty", {}))
    assert body["cursor"]["branch"] == "br_main"
    assert "world://dirty" in body["push"]


def test_the_dirty_backlog_resumes_from_a_cursor(mcp, core_server):
    url, core = core_server
    first = tool_body(mcp.call("subscribe_world_dirty", {}))
    seq = first["cursor"]["seq"]
    # someone else changes the world through the core's own door — a plain
    # token without an agent claim, exactly as §4.1 always behaved
    httpx.post(url + "/v1/vocab/call", headers=PROP, json={
        "word": "props.harbor.crates", "node": "crates_01",
        "params": {"count": 9, "style": "stained", "from_library": 0, "seed": 5}})
    assert _events(core, ["vocab.call"])[-1].actor.agent is None
    second = tool_body(mcp.call("subscribe_world_dirty", {"since_seq": seq}))
    assert second["cursor"]["seq"] > seq
    assert any(e["type"] == "version.committed" for e in second["events"])


def test_the_author_is_on_every_write(mcp, core_server):
    """The brief's rule, checked at the log: every write names its author —
    and the row alone answers *which agent acted for whom*."""
    url, core = core_server
    # the agent files through its per-agent token …
    tool_body(mcp.call("file_work_order", {
        "request": "make these sturdier", "selection": ["span_south"],
        "intent": STURDIER_INTENT}))
    # … and Casey's own hand files through the plain token (no agent claim)
    httpx.post(url + "/v1/call", headers=OWNER, json={
        "utterance": "make these sturdier",
        "view": {"branch": "br_main", "selection": ["span_south"]},
        "intent": {"spec_sentence": "reinforce the south span",
                   "estimate": STURDIER_INTENT["estimate"]}})
    rows = _events(core, ["wo.filed", "wo.compiled"])
    for ev in rows:
        assert ev.actor.id, f"{ev.type} has no author"
    # one actor (casey), two authors on the filed rows: the server's carry
    # the agent claim, Casey's own carry none — recorded, never gated (A1)
    filed = [ev for ev in rows if ev.type == "wo.filed"]
    assert {ev.actor.id for ev in filed} == {"casey"}
    assert {ev.actor.agent for ev in filed} == {"world-mcp", None}
