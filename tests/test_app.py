"""The app: the Research Desk's draft loop, the web API, and a Trial end to end on synthetic bars.
A scripted AI stands in for OmniRoute; nothing here reaches the network or MT5."""

import json
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from vibedesk.app.research import EXAMPLE, DraftFailed, draft_spec, split_reply, system_prompt
from vibedesk.app.server import create_app
from vibedesk.blocks.registry import LIBRARY
from vibedesk.market.bars import Timeframe
from vibedesk.market.cache import record_import, write_chunk
from vibedesk.market.costs import CostMeasurement, record_cost
from vibedesk.market.synthetic import SyntheticBarSource


class ScriptedLlm:
    def __init__(self, *replies: str) -> None:
        self.replies, self.calls = list(replies), []

    def chat(self, messages: list[dict]) -> str:
        self.calls.append(messages)
        return self.replies.pop(0)


def reply(spec: dict, text: str = "A Donchian breakout on GOLD H1.") -> str:
    return f"{text}\n```json\n{json.dumps(spec)}\n```"


def test_a_valid_reply_becomes_a_draft():
    draft = draft_spec(ScriptedLlm(reply(EXAMPLE)), "trade GOLD breakouts")

    assert draft.spec["entry"]["block"] == "donchian_breakout"
    assert draft.explanation == "A Donchian breakout on GOLD H1."
    assert (draft.combinations, draft.attempts) == (8, 1)


def test_an_invalid_spec_is_sent_back_with_the_errors_until_it_is_fixed():
    bad = {**EXAMPLE, "entry": {"block": "moon_phase", "params": {}}}
    llm = ScriptedLlm(reply(bad), reply(EXAMPLE))

    draft = draft_spec(llm, "trade the moon")

    assert draft.attempts == 2
    feedback = llm.calls[1][-1]["content"]
    assert "not valid" in feedback and "unknown block" in feedback


def test_a_spec_that_never_validates_fails_with_the_reason():
    llm = ScriptedLlm("no json here", "still none", "nope")

    with pytest.raises(DraftFailed, match="couldn't write a valid Spec in 3 tries"):
        draft_spec(llm, "anything")


def test_a_test_result_and_the_next_request_reach_the_ai_as_one_turn():
    llm = ScriptedLlm(reply(EXAMPLE))
    history = [{"role": "user", "content": "GOLD breakouts"},
               {"role": "assistant", "content": reply(EXAMPLE)},
               {"role": "user", "content": "Test result: REJECT, 12 trades."}]  # fmt: skip

    draft_spec(llm, "make it trade more", history)

    roles = [m["role"] for m in llm.calls[0]]
    assert roles == ["system", "user", "assistant", "user"]
    assert llm.calls[0][-1]["content"] == "Test result: REJECT, 12 trades.\n\nmake it trade more"


def test_the_prompt_lists_every_block():
    prompt = system_prompt()
    assert all(block.name in prompt for block in LIBRARY.all())


def test_a_reply_without_a_fence_still_parses():
    assert split_reply('Sure. {"a": 1} ') == ("Sure.", '{"a": 1}')


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path / "data", llm=ScriptedLlm(reply(EXAMPLE)),
                                 settings_root=tmp_path / "settings"))  # fmt: skip


def test_settings_never_send_the_key_back(client, tmp_path):
    saved = client.post("/api/settings", json={"llm_key": "sk-secret-123", "llm_model": "m1"})

    assert saved.json() == {"llm_url": "http://127.0.0.1:20128/v1", "llm_model": "m1",
                            "llm_key_set": True}  # fmt: skip
    assert "sk-secret-123" not in client.get("/api/status").text
    assert "sk-secret-123" in (tmp_path / "settings" / "settings.json").read_text()


def test_draft_then_a_trial_without_measured_costs_asks_for_an_update(client):
    draft = client.post("/api/draft", json={"message": "GOLD breakouts"}).json()
    trial = client.post("/api/trial", json={"idea": "GOLD breakouts", "spec": draft["spec"]})

    assert draft["spec"]["symbol"] == "GOLD"
    assert trial.status_code == 409 and "Update data" in trial.json()["detail"]
    assert client.get("/api/trials").json() == []


def test_a_list_the_browser_wrote_as_mixed_numbers_is_accepted():
    # JSON.stringify(2.0) is "2": the draft's [1.5, 2.0] comes back as [1.5, 2].
    from vibedesk.app.research import normalize
    from vibedesk.blocks.spec import validate

    target = {"block": "r_target", "params": {"r": [1.5, 2]}}
    spec = {**EXAMPLE, "exit": {"stop": {"block": "atr_stop", "params": {}}, "target": target}}

    assert validate(normalize(spec)).exit.target.params["r"] == [1.5, 2.0]


def test_a_bad_spec_is_refused(client):
    response = client.post("/api/trial", json={"spec": {"name": "x"}})

    assert response.status_code == 400 and "not valid" in response.json()["detail"]


def synthetic_cache(root):
    """Eleven years of synthetic GOLD M15 and H1 bars, judged, with a measured cost."""
    source = SyntheticBarSource(3, date(2013, 12, 1), date(2025, 1, 1))
    for timeframe in (Timeframe.M15, Timeframe.H1):
        bars = source.fetch("GOLD", timeframe)
        for year in range(2013, 2025):
            start = datetime(year, 1, 1, tzinfo=UTC).timestamp()
            end = datetime(year + 1, 1, 1, tzinfo=UTC).timestamp()
            write_chunk(root, bars.between(int(start), int(end)), year)
    record_import(root, "GOLD", datetime(2025, 1, 1, tzinfo=UTC))
    record_cost(root, CostMeasurement("GOLD", "2026-10-23T00:00:00+00:00", 0.01, 30.0, 35.0, 1000,
                                      -50.0, 10.0, 3))  # fmt: skip


def test_a_trial_runs_end_to_end_and_shows_in_the_history(tmp_path):
    root = tmp_path / "data"
    synthetic_cache(root)
    client = TestClient(create_app(root, llm=ScriptedLlm(), settings_root=tmp_path / "s"))
    spec = {**EXAMPLE, "entry": {"block": "donchian_breakout", "params": {"period": 20}},
            "filters": [], "exit": {"stop": {"block": "atr_stop", "params": {}}}}  # fmt: skip

    result = client.post("/api/trial", json={"idea": "breakouts", "spec": spec}).json()

    assert result["verdict"] in ("CANDIDATE", "REJECT") and result["years"][0] == 2017
    assert len(result["year_net_r"]) == 8 and result["metrics"]["trades"] > 0
    assert result["equity"][-1][1] == pytest.approx(result["metrics"]["net_r"], abs=0.01)
    assert result["cost"]["spread_floor_points"] == 30.0
    [row] = client.get("/api/trials").json()
    assert (row["id"], row["idea"], row["symbol"]) == (result["id"], "breakouts", "GOLD")
    assert client.get(f"/api/trials/{row['id']}").json()["metrics"] == result["metrics"]
    status = {d["symbol"]: d for d in client.get("/api/status").json()["data"]}
    assert status["GOLD"]["imported"] and status["GOLD"]["spread_floor"] == 30.0
