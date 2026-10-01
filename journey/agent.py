"""The care-journey agent: closes documentation gaps on a pended prior-authorization case before its CMS deadline.

It works as a task loop: a planner chooses the next tool, a guard checks the choice, the tool runs, and every step is
written to the task log. Two planners share the loop:
- "llm": an LLM with OpenAI-style function calling chooses each step from the journey state
- "rules": a deterministic playbook (used offline, in CI, and as the fallback if the LLM fails)

Guards the planner cannot bypass:
- only the listed tools; records are requested only after the provider is verified, and only for facts actually missing
- identical outbound messages are never queued twice (idempotency key)
- if the case is at risk or breached, or its criteria were not met, it must be escalated to a clinician before finishing
- the loop cannot finish while a missing fact has neither been requested nor escalated
"""
import hashlib
import json
import os
import time
from datetime import timedelta
from pathlib import Path

import httpx

from journey import sla, tools
from journey.store import JourneyStore, TaskLog, now

MAX_STEPS = 12  # a full journey is 5-6 steps; the rest is headroom for blocked or repaired steps
CACHE = Path(__file__).resolve().parents[1] / ".cache" / "llm"
SYSTEM = """You are a prior-authorization care-journey agent working for a utilization management team.
A request was pended. Your job: make sure the case can be completed before its CMS decision deadline.
Use one tool per turn. Verify the ordering provider before requesting records. Request exactly the missing facts.
Escalate to a clinician only when the state says escalation_required is true (SLA at risk or breached, criteria not
met, or an invalid provider); clinician time is scarce, so otherwise request the records yourself.
Tell the member what is happening in one or two plain sentences. Call finish when nothing else is needed.
The state lists your outstanding obligations; complete each of them exactly once, in a sensible order, then finish.
Never invent facts or contact details; only use tool results."""


class GuardError(Exception):
    pass


class JourneyAgent:
    def __init__(self, store: JourneyStore, log: TaskLog, planner: str = "rules", http=None, clock=now):
        self.store, self.log, self.planner, self.http, self.clock = store, log, planner, http, clock

    # ---------- state and guards ----------
    def _state(self, j, s):
        st = {"journey_id": j["journey_id"], "policy_id": j["policy_id"], "ordering_npi": self._npi,
              "pend_reason": j.get("pend_reason"),
              "missing_facts": j["missing_facts"], "sla": sla.status(j["received_at"], j["urgency"], self.clock()),
              "provider": s["provider"], "policy_checked": s["policy"] is not None, "requested": sorted(s["requested"]),
              "escalated": s["escalated"], "member_notified": s["notified"]}
        st["escalation_required"] = self._must_escalate(st)
        st["escalation_reason"] = self._why(st) if st["escalation_required"] else None
        st["outstanding"] = self._outstanding(st)
        return st

    @staticmethod
    def _outstanding(st):
        """Obligations still open, computed deterministically. The planner decides order and wording, not what is owed."""
        todo = []
        if st["provider"] is None:
            todo.append("verify_provider")
        if not st["policy_checked"]:
            todo.append("coverage_policy")
        if st["escalation_required"] and not st["escalated"]:
            todo.append("escalate_to_clinician")
        unrequested = [f for f in st["missing_facts"] if f not in st["requested"]]
        if unrequested and (st["provider"] is None or st["provider"].get("valid")):
            todo.append(f"request_records: {unrequested}")
        if not st["member_notified"]:
            todo.append("notify_member")
        return todo or ["finish"]

    @staticmethod
    def _must_escalate(st):
        return (st["sla"]["status"] in ("at_risk", "breached") or st["pend_reason"] == "criteria_not_met"
                or (st["provider"] is not None and not st["provider"].get("valid")))

    @staticmethod
    def _why(st):
        if st["pend_reason"] == "criteria_not_met":
            return "criteria not met: clinical judgement needed"
        if st["provider"] is not None and not st["provider"].get("valid"):
            return "ordering provider not found in NPPES"
        return f"SLA {st['sla']['status']}: {st['sla']['hours_left']}h left"

    def _guard(self, tool, args, st):
        allowed = {t["function"]["name"] for t in tools.TOOL_SCHEMAS}
        if tool not in allowed:
            raise GuardError(f"tool {tool!r} is not allowed")
        if tool == "verify_provider" and args.get("npi") != st["ordering_npi"]:
            raise GuardError(f"verify the case's ordering provider (NPI {st['ordering_npi']}), not another identifier")
        if tool == "coverage_policy" and args.get("policy_id") != st["policy_id"]:
            raise GuardError(f"this case is judged against {st['policy_id']}")
        if tool == "request_records":
            if not (st["provider"] and st["provider"].get("valid")):
                raise GuardError("verify the ordering provider before requesting records")
            extra = set(args.get("missing_facts", [])) - set(st["missing_facts"])
            if extra:
                raise GuardError(f"not missing, do not request: {sorted(extra)}")
        done = {"verify_provider": st["provider"] is not None, "coverage_policy": st["policy_checked"],
                "notify_member": st["member_notified"], "escalate_to_clinician": st["escalated"]}
        if done.get(tool):
            raise GuardError(f"{tool} is already done; next: {st['outstanding']}")
        if tool == "escalate_to_clinician" and not st["escalation_required"]:
            raise GuardError("escalation not required: request the missing records instead (clinician time is reserved "
                             "for at-risk SLAs, unmet criteria and unknown providers)")
        if tool == "finish" and st["outstanding"] != ["finish"]:
            raise GuardError(f"cannot finish yet, outstanding: {st['outstanding']}")

    # ---------- planners ----------
    def _rules_next(self, st):
        if st["provider"] is None:
            return "verify_provider", {"npi": self._npi}
        if not st["policy_checked"]:
            return "coverage_policy", {"policy_id": st["policy_id"]}
        if st["escalation_required"] and not st["escalated"]:
            return "escalate_to_clinician", {"reason": st["escalation_reason"]}
        todo = [f for f in st["missing_facts"] if f not in st["requested"]]
        if todo and st["provider"].get("valid"):
            return "request_records", {"missing_facts": todo, "channel": "fax" if st["provider"].get("fax") else "portal"}
        if not st["member_notified"]:
            msg = ("We have asked your doctor's office for more information to finish reviewing your request."
                   if st["requested"] else "Your request is with a clinician for review.")
            return "notify_member", {"message": msg}
        return "finish", {"summary": "all gaps requested or escalated"}

    def _llm_call(self, messages):
        key = hashlib.sha256(json.dumps(messages, default=str).encode()).hexdigest()
        path = CACHE / f"{key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        body = {"model": os.getenv("LLM_MODEL", "openai/gpt-oss-120b"), "messages": messages, "tools": tools.TOOL_SCHEMAS,
                "tool_choice": "required", "temperature": 0}
        for attempt in range(6):  # free-tier rate limits: back off and retry
            r = httpx.post(os.getenv("LLM_API_URL", "https://api.groq.com/openai/v1/chat/completions"), json=body, timeout=120,
                           headers={"Authorization": f"Bearer {os.getenv('LLM_API_KEY') or os.environ['GROQ_API_KEY']}"})
            if r.status_code != 429:
                break
            time.sleep(float(r.headers.get("retry-after", 5 * (attempt + 1))))
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(msg), encoding="utf-8")
        return msg

    def _llm_next(self, st, history):
        user = {"role": "user", "content": f"Journey state:\n{json.dumps(st, default=str)}\n\nChoose the next tool."}
        msg = self._llm_call([{"role": "system", "content": SYSTEM}] + history + [user])
        call = msg["tool_calls"][0]["function"]
        return call["name"], json.loads(call.get("arguments") or "{}")

    # ---------- tools ----------
    def _run_tool(self, j, tool, args, s):
        if tool == "verify_provider":
            s["provider"] = tools.verify_provider(args["npi"], self.http)
            self.store.update(j["journey_id"], provider=s["provider"])
            return s["provider"]
        if tool == "coverage_policy":
            s["policy"] = tools.coverage_policy(args["policy_id"], self.http)
            return {k: v for k, v in s["policy"].items() if k != "indications_excerpt"}
        if tool == "request_records":
            facts = [f for f in args["missing_facts"] if f not in s["requested"]]
            body = tools.records_request_text(j["journey_id"], s["provider"], facts)
            key = f"{j['journey_id']}|records|{'+'.join(sorted(facts))}"
            queued = self.store.queue_message(j["journey_id"], "records_request",
                                              f"{s['provider']['name']} ({args.get('channel', 'fax')} {s['provider'].get('fax') or ''})".strip(),
                                              body, key)
            s["requested"].update(facts)
            deadline = sla.due_at(j["received_at"], j["urgency"])
            follow = min(self.clock() + timedelta(hours=24), deadline - timedelta(hours=12))
            self.log.add_followup(j["journey_id"], follow.isoformat(), "records")
            self.store.update(j["journey_id"], status="awaiting_records")
            return {"queued": queued, "facts": facts, "follow_up_at": follow.isoformat()}
        if tool == "notify_member":
            queued = self.store.queue_message(j["journey_id"], "member_update", "member", args["message"], f"{j['journey_id']}|member")
            s["notified"] = True
            return {"queued": queued}
        if tool == "escalate_to_clinician":
            queued = self.store.queue_message(j["journey_id"], "clinician_escalation", "clinical review queue",
                                              args["reason"], f"{j['journey_id']}|escalate")
            s["escalated"] = True
            self.store.update(j["journey_id"], status="escalated")
            return {"queued": queued}
        return {"finished": True, "summary": args.get("summary", "")}

    # ---------- loop ----------
    def run(self, journey_id: str, ordering_npi: str) -> dict:
        j = self.store.get(journey_id)
        self._npi = ordering_npi
        s = {"provider": None, "policy": None, "requested": set(), "escalated": False, "notified": False}
        trace, history = [], []
        planner = self.planner
        for seq in range(1, MAX_STEPS + 1):
            st = self._state(j, s)
            try:
                tool, args = self._llm_next(st, history) if planner == "llm" else self._rules_next(st)
            except Exception as e:  # LLM unavailable or malformed: the playbook takes over
                trace.append({"step": seq, "planner": planner, "error": f"planner failed ({type(e).__name__}); rules take over"})
                planner = "rules"
                tool, args = self._rules_next(st)
            try:
                self._guard(tool, args, st)
                result, ok = self._run_tool(j, tool, args, s), True
            except (GuardError, tools.ToolError, httpx.HTTPError) as e:
                result, ok = {"error": str(e)}, False
            self.log.log(journey_id, seq, tool, args, result, ok)
            trace.append({"step": seq, "planner": planner, "tool": tool, "args": args, "ok": ok, "result": result})
            history += [{"role": "assistant", "content": None, "tool_calls": [{"id": f"c{seq}", "type": "function",
                         "function": {"name": tool, "arguments": json.dumps(args)}}]},
                        {"role": "tool", "tool_call_id": f"c{seq}", "content": json.dumps(result, default=str)[:1500]}]
            if tool == "finish" and ok:
                break
        final = self.store.get(journey_id)
        return {"journey_id": journey_id, "status": final["status"], "sla": sla.status(j["received_at"], j["urgency"], self.clock()),
                "steps": len(trace), "trace": trace, "outbox": final["outbox"]}

    def records_received(self, journey_id: str, facts: list[str]) -> dict:
        j = self.store.get(journey_id)
        got = sorted(set(j["received_facts"]) | set(facts))
        done = set(j["missing_facts"]) <= set(got)
        self.store.update(journey_id, received_facts=got, status="ready_for_review" if done else j["status"])
        if done:
            self.log.close_followup(journey_id, "records")
        return self.store.get(journey_id)
