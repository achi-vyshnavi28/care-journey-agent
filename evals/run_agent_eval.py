"""Run the agent on the 16 real pended cases with live NPPES and CMS Coverage API calls, and score each journey.

    python -m evals.run_agent_eval            # rules and llm planners (llm needs GROQ_API_KEY; responses cached)

A journey is correct when: every missing fact was requested from a verified provider (unless escalation is required),
escalation happened exactly when the policy requires it (SLA at risk or breached, criteria not met, unknown provider),
the member was told, and the journey finished.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from moto import mock_aws

from journey import sla
from journey.agent import JourneyAgent
from journey.store import JourneyStore, TaskLog

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def score(case, j, out):
    used = [s["tool"] for s in out["trace"] if s.get("ok")]
    st = sla.status(j["received_at"], j["urgency"], NOW)["status"]
    must_escalate = st in ("at_risk", "breached") or case["pend_reason"] == "criteria_not_met"
    requested = set()
    for s in out["trace"]:
        if s.get("ok") and s["tool"] == "request_records":
            requested |= set(s["result"]["facts"])
    checks = {
        "requested_all_missing": set(case["missing_facts"]) <= requested,
        "escalation_correct": ("escalate_to_clinician" in used) == must_escalate,
        "member_notified": "notify_member" in used,
        "finished": bool(used) and used[-1] == "finish",
    }
    return {"case_id": case["case_id"], "sla": st, "must_escalate": must_escalate, **checks, "correct": all(checks.values()),
            "steps": out["steps"], "blocked_by_guards": sum(1 for s in out["trace"] if s.get("tool") and not s["ok"]),
            "fallbacks": sum(1 for s in out["trace"] if "error" in s and "tool" not in s), "tools": used}


def run(planner):
    cases = json.loads((ROOT / "data" / "pended_cases.json").read_text(encoding="utf-8"))
    rows = []
    with mock_aws():
        TaskLog.create_table("tasks", boto3.client("dynamodb", region_name="us-east-1"))
        log = TaskLog(boto3.resource("dynamodb", region_name="us-east-1").Table("tasks"))
        store = JourneyStore("sqlite://")
        agent = JourneyAgent(store, log, planner=planner, clock=lambda: NOW)
        for c in cases:
            d = c["demo"]
            j = {"journey_id": c["case_id"], "policy_id": c["policy_id"], "urgency": d["urgency"],
                 "received_at": NOW - timedelta(hours=d["received_hours_ago"]), "pend_reason": c["pend_reason"],
                 "missing_facts": c["missing_facts"]}
            store.create(j)
            rows.append(score(c, j, agent.run(c["case_id"], d["ordering_npi"])))
    n = len(rows)
    summary = {"planner": planner, "journeys": n, "correct": sum(r["correct"] for r in rows),
               "escalation_correct": sum(r["escalation_correct"] for r in rows),
               "requested_all_missing": sum(r["requested_all_missing"] for r in rows),
               "unsafe_steps_blocked": sum(r["blocked_by_guards"] for r in rows),
               "planner_fallbacks": sum(r["fallbacks"] for r in rows),
               "avg_steps": round(sum(r["steps"] for r in rows) / n, 1)}
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / f"agent_eval_{planner}.json").write_text(json.dumps({"summary": summary, "journeys": rows}, indent=1), encoding="utf-8")
    return summary, rows


if __name__ == "__main__":
    for p in sys.argv[1:] or ["rules", "llm"]:
        s, rows = run(p)
        print(json.dumps(s))
        for r in rows:
            if not r["correct"] or r["blocked_by_guards"]:
                print("   ", r["case_id"], r["sla"], {k: r[k] for k in ("requested_all_missing", "escalation_correct", "member_notified", "finished", "blocked_by_guards")}, r["tools"])
