"""AWS Lambda entry point: SQS event source -> journey agent -> PostgreSQL/MySQL journeys + DynamoDB task log.

Configured by environment: JOURNEY_DB_URL, JOURNEY_DYNAMO_TABLE, JOURNEY_PLANNER (rules by default).
The event source mapping must enable ReportBatchItemFailures (see deploy/template.yaml).
"""
import os
from functools import lru_cache

from journey.agent import JourneyAgent
from journey.queue import process
from journey.store import JourneyStore, TaskLog


@lru_cache  # reused across warm invocations of the same Lambda container
def _deps():
    store, log = JourneyStore(), TaskLog()
    return store, JourneyAgent(store, log, planner=os.getenv("JOURNEY_PLANNER", "rules"))


def handler(event, context=None):
    store, agent = _deps()
    out = process(event.get("Records", []), store, agent)
    print({"processed": len(out["processed"]), "skipped": len(out["skipped"]), "failed": len(out["batchItemFailures"])})
    return {"batchItemFailures": out["batchItemFailures"]}
