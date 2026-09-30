"""Shared fixtures: an HTTP client that replays real, recorded NPPES and CMS Coverage API responses, a DynamoDB task
log on moto, and the relational store on SQLite (plus PostgreSQL / MySQL when their URLs are set, e.g. in CI)."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import httpx
import pytest
from moto import mock_aws

from journey.store import JourneyStore, TaskLog, metadata

FIX = Path(__file__).with_name("fixtures")
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8-sig"))


def _handler(request: httpx.Request) -> httpx.Response:
    q = dict(request.url.params)
    if "npiregistry" in request.url.host:
        f = FIX / f"nppes_{q.get('number')}.json"
        return httpx.Response(200, json=_load(f.name) if f.exists() else _load("nppes_invalid.json"))
    if "coverage.cms.gov" in request.url.host:
        return httpx.Response(200, json=_load(f"cms_ncd_{q['ncdid']}_v{q['ncdver']}.json"))
    return httpx.Response(404)


@pytest.fixture
def http():
    return httpx.Client(transport=httpx.MockTransport(_handler))


DB_URLS = ["sqlite://"] + [os.environ[k] for k in ("JOURNEY_TEST_PG_URL", "JOURNEY_TEST_MYSQL_URL") if os.getenv(k)]


@pytest.fixture(params=DB_URLS, ids=lambda u: u.split(":")[0])
def stores(request, monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t"}.items():
        monkeypatch.setenv(k, v)
    store = JourneyStore(request.param)
    metadata.drop_all(store.engine)
    metadata.create_all(store.engine)
    with mock_aws():
        TaskLog.create_table("tasks", boto3.client("dynamodb"))
        yield store, TaskLog(boto3.resource("dynamodb").Table("tasks"))


def make_journey(store, jid="MTS-0013", policy="ncd_100_1_bariatric", reason="missing_documentation",
                 missing=("prior_medical_treatment_failed",), urgency="standard", hours_ago=10):
    store.create({"journey_id": jid, "policy_id": policy, "urgency": urgency, "received_at": NOW - timedelta(hours=hours_ago),
                  "pend_reason": reason, "missing_facts": list(missing)})
    return jid
