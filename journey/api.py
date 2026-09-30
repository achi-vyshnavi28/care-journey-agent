"""REST API for care journeys on pended prior-authorization cases.

    uvicorn journey.api:app --port 8930
    POST /journeys                       start a journey for a pended case; the agent runs and returns its trace
    GET  /journeys                       all journeys with CMS-0057-F SLA status, most urgent first
    GET  /journeys/{id}                  journey, agent task log (DynamoDB), outbox
    POST /journeys/{id}/records-received documents arrived for some facts; ready for review when all are in
    GET  /followups                      open follow-ups across journeys, soonest first (DynamoDB GSI)
"""
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from journey import sla
from journey.agent import JourneyAgent
from journey.store import JourneyStore, TaskLog

log = logging.getLogger("journey")
logging.basicConfig(level=logging.INFO, format="%(message)s")
app = FastAPI(title="Care Journey Agent", description="Task-driven agents that close prior-authorization documentation gaps before the CMS deadline")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("JOURNEY_CORS", "http://localhost:5176").split(","),
                   allow_origin_regex=os.getenv("JOURNEY_CORS_REGEX"),  # e.g. https://.*\.onrender\.com when deployed
                   allow_methods=["GET", "POST"], allow_headers=["Content-Type"])


@lru_cache
def deps():
    store = JourneyStore()
    if os.getenv("JOURNEY_DYNAMO_TABLE"):
        tasks = TaskLog()
    else:  # no AWS configured: a local DynamoDB emulator so the API runs anywhere
        import boto3
        from moto import mock_aws

        deps.mock = mock_aws()
        deps.mock.start()
        client = boto3.client("dynamodb", region_name="us-east-1")
        TaskLog.create_table("journey-tasks", client)
        tasks = TaskLog(boto3.resource("dynamodb", region_name="us-east-1").Table("journey-tasks"))
    return store, tasks, JourneyAgent(store, tasks, planner=os.getenv("JOURNEY_PLANNER", "rules"))


@app.middleware("http")
async def request_log(request: Request, call_next):
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    t = time.perf_counter()
    response = await call_next(request)
    response.headers["x-request-id"] = rid
    log.info('{"request_id": "%s", "path": "%s", "status": %d, "ms": %.1f}', rid, request.url.path,
             response.status_code, (time.perf_counter() - t) * 1000)
    return response


class JourneyIn(BaseModel):
    case_id: str = Field(min_length=1, max_length=40)
    policy_id: Literal["ncd_100_1_bariatric", "ncd_240_4_cpap"]
    urgency: Literal["standard", "expedited"] = "standard"
    received_at: datetime | None = None
    pend_reason: Literal["missing_documentation", "criteria_not_met"]
    missing_facts: list[str] = []
    ordering_npi: str = Field(pattern=r"^\d{10}$")


class RecordsIn(BaseModel):
    facts: list[str] = Field(min_length=1)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/journeys", status_code=201)
def start(body: JourneyIn):
    store, _, agent = deps()
    if store.get(body.case_id):
        raise HTTPException(409, "journey already exists")
    store.create({"journey_id": body.case_id, "policy_id": body.policy_id, "urgency": body.urgency,
                  "received_at": body.received_at or datetime.now(timezone.utc), "pend_reason": body.pend_reason,
                  "missing_facts": body.missing_facts})
    return agent.run(body.case_id, body.ordering_npi)


@app.get("/journeys")
def board():
    store, _, _ = deps()
    rows = []
    for j in store.all():
        s = sla.status(j["received_at"], j["urgency"], closed=j["status"] == "ready_for_review")
        rows.append({**j, "sla": s})
    return sorted(rows, key=lambda r: (r["sla"]["status"] == "closed", r["sla"]["hours_left"]))


@app.get("/journeys/{journey_id}")
def one(journey_id: str):
    store, tasks, _ = deps()
    j = store.get(journey_id)
    if j is None:
        raise HTTPException(404, "unknown journey")
    return {**j, "sla": sla.status(j["received_at"], j["urgency"], closed=j["status"] == "ready_for_review"),
            "tasks": tasks.tasks(journey_id)}


@app.post("/journeys/{journey_id}/records-received")
def records(journey_id: str, body: RecordsIn):
    store, _, agent = deps()
    j = store.get(journey_id)
    if j is None:
        raise HTTPException(404, "unknown journey")
    unknown = set(body.facts) - set(j["missing_facts"])
    if unknown:
        raise HTTPException(422, f"not requested for this journey: {sorted(unknown)}")
    return agent.records_received(journey_id, body.facts)


@app.get("/followups")
def followups():
    return deps()[1].open_followups()


@app.on_event("startup")
def demo_seed():
    """JOURNEY_DEMO=1: start journeys for the 16 real pended cases (demo arrival times from data/pended_cases.json)."""
    if os.getenv("JOURNEY_DEMO") != "1":
        return
    import json
    from pathlib import Path

    from journey.store import metadata

    store, _, agent = deps()
    metadata.drop_all(store.engine)
    metadata.create_all(store.engine)
    cases = json.loads((Path(__file__).resolve().parents[1] / "data" / "pended_cases.json").read_text(encoding="utf-8"))
    for c in cases:
        d = c["demo"]
        store.create({"journey_id": c["case_id"], "policy_id": c["policy_id"], "urgency": d["urgency"],
                      "received_at": datetime.now(timezone.utc) - timedelta(hours=d["received_hours_ago"]),
                      "pend_reason": c["pend_reason"], "missing_facts": c["missing_facts"]})
        agent.run(c["case_id"], d["ordering_npi"])
