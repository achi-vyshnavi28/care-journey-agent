"""Persistence.

- DynamoDB (agent task log and follow-ups), single-table:
    PK = JOURNEY#<id>   SK = TASK#<seq>        every tool call the agent made, in order (the audit of the agent)
    GSI1PK = FOLLOWUP#open  GSI1SK = <due_at>  open follow-ups across all journeys, soonest first (the scheduler's query)
- Relational (SQLAlchemy Core; PostgreSQL or MySQL in production, SQLite locally): journeys and an outbox of queued
  outbound messages with a unique idempotency key, so a retried agent step can never send the same request twice.
"""
import json
import os
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, UniqueConstraint, create_engine, insert, select, update
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
journeys = Table(
    "journeys", metadata,
    Column("journey_id", String(40), primary_key=True),
    Column("policy_id", String(60), nullable=False),
    Column("urgency", String(10), nullable=False),
    Column("received_at", DateTime(timezone=True), nullable=False),
    Column("pend_reason", String(40)),
    Column("missing_facts", JSON, nullable=False),
    Column("received_facts", JSON, nullable=False),
    Column("provider", JSON),
    Column("status", String(30), nullable=False),   # open | awaiting_records | escalated | ready_for_review
    Column("updated_at", DateTime(timezone=True), nullable=False),
)
outbox = Table(
    "outbox", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("journey_id", String(40), nullable=False, index=True),
    Column("kind", String(30), nullable=False),     # records_request | member_update | clinician_escalation
    Column("recipient", String(200), nullable=False),
    Column("body", Text, nullable=False),
    Column("idempotency_key", String(200), nullable=False),
    Column("status", String(20), nullable=False),   # queued (a human or integration sends it)
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("idempotency_key", name="uq_outbox_idempotency"),
)


def now():
    return datetime.now(timezone.utc)


def _as_utc(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class JourneyStore:
    def __init__(self, url=None):
        self.engine = create_engine(url or os.getenv("JOURNEY_DB_URL", "sqlite:///journeys.sqlite3"), future=True)
        metadata.create_all(self.engine)

    def create(self, j: dict):
        with self.engine.begin() as c:
            c.execute(insert(journeys).values(journey_id=j["journey_id"], policy_id=j["policy_id"], urgency=j["urgency"],
                                              received_at=j["received_at"], pend_reason=j.get("pend_reason"),
                                              missing_facts=j["missing_facts"], received_facts=[], provider=None,
                                              status="open", updated_at=now()))

    def update(self, journey_id, **values):
        with self.engine.begin() as c:
            c.execute(update(journeys).where(journeys.c.journey_id == journey_id).values(**values, updated_at=now()))

    def get(self, journey_id):
        with self.engine.connect() as c:
            row = c.execute(select(journeys).where(journeys.c.journey_id == journey_id)).first()
            if row is None:
                return None
            box = c.execute(select(outbox).where(outbox.c.journey_id == journey_id).order_by(outbox.c.id))
            j = dict(row._mapping)
            j["received_at"] = _as_utc(j["received_at"])
            return {**j, "outbox": [dict(r._mapping) for r in box]}

    def all(self):
        with self.engine.connect() as c:
            rows = [dict(r._mapping) for r in c.execute(select(journeys))]
        for r in rows:
            r["received_at"] = _as_utc(r["received_at"])
        return rows

    def queue_message(self, journey_id, kind, recipient, body, idempotency_key) -> bool:
        """Returns False if this exact message was already queued (idempotent retries)."""
        try:
            with self.engine.begin() as c:
                c.execute(insert(outbox).values(journey_id=journey_id, kind=kind, recipient=recipient, body=body,
                                                idempotency_key=idempotency_key, status="queued", created_at=now()))
            return True
        except IntegrityError:
            return False


class TaskLog:
    def __init__(self, table=None, table_name=None):
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(table_name or os.environ["JOURNEY_DYNAMO_TABLE"])
        self.table = table

    @staticmethod
    def create_table(name, client):
        client.create_table(
            TableName=name, BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("PK", "SK", "GSI1PK", "GSI1SK")],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[{"IndexName": "GSI1", "Projection": {"ProjectionType": "ALL"},
                                     "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"},
                                                   {"AttributeName": "GSI1SK", "KeyType": "RANGE"}]}])

    def log(self, journey_id, seq, tool, args, result, ok=True):
        self.table.put_item(Item={"PK": f"JOURNEY#{journey_id}", "SK": f"TASK#{seq:04d}", "tool": tool,
                                  "args": json.dumps(args), "result": json.dumps(result, default=str)[:4000],
                                  "ok": ok, "at": now().isoformat()})

    def add_followup(self, journey_id, due_at: str, what: str):
        self.table.put_item(Item={"PK": f"JOURNEY#{journey_id}", "SK": f"FOLLOWUP#{what}", "GSI1PK": "FOLLOWUP#open",
                                  "GSI1SK": due_at, "journey_id": journey_id, "what": what, "due_at": due_at})

    def close_followup(self, journey_id, what: str):
        self.table.update_item(Key={"PK": f"JOURNEY#{journey_id}", "SK": f"FOLLOWUP#{what}"},
                               UpdateExpression="REMOVE GSI1PK, GSI1SK SET closed_at = :t",
                               ExpressionAttributeValues={":t": now().isoformat()})

    def tasks(self, journey_id):
        from boto3.dynamodb.conditions import Key

        r = self.table.query(KeyConditionExpression=Key("PK").eq(f"JOURNEY#{journey_id}") & Key("SK").begins_with("TASK#"))
        return [{**i, "args": json.loads(i["args"]), "result": json.loads(i["result"])} for i in r["Items"]]

    def open_followups(self, limit=50):
        from boto3.dynamodb.conditions import Key

        r = self.table.query(IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq("FOLLOWUP#open"), Limit=limit)
        return r["Items"]
