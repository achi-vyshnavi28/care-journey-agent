import json
from datetime import timedelta

import pytest

from journey import sla, tools
from journey.agent import JourneyAgent
from tests.conftest import NOW, make_journey

BARI_NPI, SLEEP_NPI = "1497941645", "1720329949"


# ---------- SLA (CMS-0057-F) ----------

@pytest.mark.parametrize("urgency, hours_ago, state", [
    ("standard", 10, "on_track"), ("standard", 7 * 24 - 30, "at_risk"), ("standard", 7 * 24 + 1, "breached"),
    ("expedited", 10, "on_track"), ("expedited", 60, "at_risk"), ("expedited", 73, "breached"),
])
def test_sla(urgency, hours_ago, state):
    assert sla.status(NOW - timedelta(hours=hours_ago), urgency, NOW)["status"] == state


def test_sla_windows_are_72h_and_7_days():
    assert sla.due_at(NOW, "expedited") - NOW == timedelta(hours=72)
    assert sla.due_at(NOW, "standard") - NOW == timedelta(days=7)


# ---------- real tools, replayed ----------

def test_verify_provider_on_real_nppes_record(http):
    p = tools.verify_provider(BARI_NPI, http)
    assert p["valid"] and p["name"] == "BARIATRIC & GI SURGERY OF THE UNIVERSITY OF ROCHESTER" and p["taxonomy"] == "Surgery"
    assert p["fax"] == "585-341-6544"
    assert tools.verify_provider("1234567890", http) == {"npi": "1234567890", "valid": False}
    with pytest.raises(tools.ToolError):
        tools.verify_provider("12345", http)


def test_coverage_policy_on_real_cms_ncd(http):
    p = tools.coverage_policy("ncd_240_4_cpap", http)
    assert p["ncd"] == "240.4" and "Continuous Positive Airway Pressure" in p["title"]
    assert "patients with OSA" in p["indications_excerpt"]
    assert "<" not in p["indications_excerpt"] and "&lt;" not in p["indications_excerpt"]  # entity-escaped HTML decoded


# ---------- the agent ----------

def agent(stores, http, planner="rules"):
    store, log = stores
    return JourneyAgent(store, log, planner=planner, http=http, clock=lambda: NOW)


def test_missing_documentation_is_requested_from_the_verified_provider(stores, http):
    store, log = stores
    jid = make_journey(store)
    out = agent(stores, http).run(jid, BARI_NPI)
    assert [s["tool"] for s in out["trace"]] == ["verify_provider", "coverage_policy", "request_records", "notify_member", "finish"]
    assert all(s["ok"] for s in out["trace"])
    req = next(m for m in out["outbox"] if m["kind"] == "records_request")
    assert "prior supervised medical weight-loss treatment" in req["body"] and "585-341-6544" in req["recipient"]
    assert out["status"] == "awaiting_records"
    assert [f["journey_id"] for f in log.open_followups()] == [jid]
    assert len(log.tasks(jid)) == 5


def test_criteria_not_met_is_escalated_not_requested(stores, http):
    store, _ = stores
    jid = make_journey(store, "MTS-1467", "ncd_240_4_cpap", "criteria_not_met", missing=())
    out = agent(stores, http).run(jid, SLEEP_NPI)
    tools_used = [s["tool"] for s in out["trace"]]
    assert "escalate_to_clinician" in tools_used and "request_records" not in tools_used
    assert out["status"] == "escalated"


def test_at_risk_case_is_escalated_and_still_requested(stores, http):
    store, _ = stores
    jid = make_journey(store, "MTS-1308", "ncd_240_4_cpap", missing=("ahi",), urgency="expedited", hours_ago=60)
    out = agent(stores, http).run(jid, SLEEP_NPI)
    tools_used = [s["tool"] for s in out["trace"]]
    assert tools_used.index("escalate_to_clinician") < tools_used.index("request_records")
    assert out["sla"]["status"] == "at_risk"


def test_unknown_provider_is_escalated(stores, http):
    store, _ = stores
    jid = make_journey(store)
    out = agent(stores, http).run(jid, "1234567890")
    tools_used = [s["tool"] for s in out["trace"]]
    assert "escalate_to_clinician" in tools_used and "request_records" not in tools_used


def test_rerun_never_queues_the_same_request_twice(stores, http):
    store, _ = stores
    jid = make_journey(store)
    agent(stores, http).run(jid, BARI_NPI)
    out = agent(stores, http).run(jid, BARI_NPI)
    assert sum(m["kind"] == "records_request" for m in out["outbox"]) == 1
    assert next(s for s in out["trace"] if s["tool"] == "request_records")["result"]["queued"] is False


def test_guards_stop_an_llm_planner_from_unsafe_steps(stores, http, monkeypatch):
    """A scripted LLM tries six unsafe moves; every one is blocked and logged, then it completes the journey."""
    store, log = stores
    jid = make_journey(store)
    script = iter([
        ("request_records", {"missing_facts": ["prior_medical_treatment_failed"]}),   # before verifying the provider
        ("delete_case", {}),                                                            # not a tool
        ("verify_provider", {"npi": BARI_NPI}),
        ("request_records", {"missing_facts": ["bmi"]}),                               # not missing
        ("escalate_to_clinician", {"reason": "unsure"}),                               # not required: wastes clinician time
        ("finish", {"summary": "done"}),                                                # obligations outstanding
        ("coverage_policy", {"policy_id": "ncd_100_1_bariatric"}),
        ("request_records", {"missing_facts": ["prior_medical_treatment_failed"]}),
        ("notify_member", {"message": "We asked your doctor for records."}),
        ("notify_member", {"message": "Again."}),                                      # repeat
        ("finish", {"summary": "done"}),
    ])
    a = agent(stores, http, planner="llm")
    monkeypatch.setattr(a, "_llm_next", lambda st, history: next(script))
    out = a.run(jid, BARI_NPI)
    blocked = [s for s in out["trace"] if not s["ok"]]
    errors = [b["result"]["error"] for b in blocked]
    assert len(blocked) == 6
    assert "verify the ordering provider" in errors[0] and "not allowed" in errors[1] and "not missing" in errors[2]
    assert "escalation not required" in errors[3] and "cannot finish yet" in errors[4] and "already done" in errors[5]
    assert out["trace"][-1]["tool"] == "finish" and out["trace"][-1]["ok"]
    assert sum(m["kind"] == "member_update" for m in out["outbox"]) == 1


def test_agent_cannot_verify_an_invented_npi(stores, http, monkeypatch):
    """Found in evaluation: an LLM without the NPI in its state invented '0000000000', got 'not found', and escalated.
    The guard now only allows the case's own ordering NPI."""
    store, _ = stores
    jid = make_journey(store)
    script = iter([("verify_provider", {"npi": "0000000000"}), ("verify_provider", {"npi": BARI_NPI}),
                   ("coverage_policy", {"policy_id": "ncd_240_4_cpap"}), ("coverage_policy", {"policy_id": "ncd_100_1_bariatric"}),
                   ("request_records", {"missing_facts": ["prior_medical_treatment_failed"]}),
                   ("notify_member", {"message": "We asked your doctor for records."}), ("finish", {"summary": "ok"})])
    a = agent(stores, http, planner="llm")
    monkeypatch.setattr(a, "_llm_next", lambda st, history: next(script))
    out = a.run(jid, BARI_NPI)
    errors = [s["result"]["error"] for s in out["trace"] if not s["ok"]]
    assert "not another identifier" in errors[0] and "judged against ncd_100_1_bariatric" in errors[1]
    assert out["status"] == "awaiting_records" and out["trace"][-1]["ok"]


def test_llm_failure_falls_back_to_the_playbook(stores, http, monkeypatch):
    store, _ = stores
    jid = make_journey(store)
    a = agent(stores, http, planner="llm")

    def boom(st, history):
        raise TimeoutError("LLM down")
    monkeypatch.setattr(a, "_llm_next", boom)
    out = a.run(jid, BARI_NPI)
    assert "rules take over" in out["trace"][0]["error"] and out["status"] == "awaiting_records"


def test_records_received_closes_the_followup(stores, http):
    store, log = stores
    jid = make_journey(store, missing=("bmi", "prior_medical_treatment_failed"))
    a = agent(stores, http)
    a.run(jid, BARI_NPI)
    assert a.records_received(jid, ["bmi"])["status"] == "awaiting_records"
    assert a.records_received(jid, ["prior_medical_treatment_failed"])["status"] == "ready_for_review"
    assert log.open_followups() == []


def test_llm_planner_tool_call_format(stores, http, monkeypatch):
    """The real LLM path parses OpenAI-style tool calls; here the HTTP layer is stubbed with that exact shape."""
    store, _ = stores
    jid = make_journey(store)
    a = agent(stores, http, planner="llm")
    calls = iter(["verify_provider", "coverage_policy", "request_records", "notify_member", "finish"])
    args = {"verify_provider": {"npi": BARI_NPI}, "coverage_policy": {"policy_id": "ncd_100_1_bariatric"},
            "request_records": {"missing_facts": ["prior_medical_treatment_failed"]},
            "notify_member": {"message": "We asked your doctor for records."}, "finish": {"summary": "ok"}}

    def fake_call(messages):
        name = next(calls)
        return {"role": "assistant", "tool_calls": [{"id": "x", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args[name])}}]}
    monkeypatch.setattr(a, "_llm_call", fake_call)
    out = a.run(jid, BARI_NPI)
    assert [s["planner"] for s in out["trace"]] == ["llm"] * 5 and all(s["ok"] for s in out["trace"])
