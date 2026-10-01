"""SQS intake + Lambda worker on moto: messages go through a real (emulated) queue and the handler gets the same event
shape AWS sends. Covers the happy path, redelivery idempotency, partial batch failures and the dead-letter queue."""
import json

import boto3

from journey import worker
from journey.agent import JourneyAgent
from journey.queue import JourneyQueue, create_queues, process
from tests.conftest import NOW

BARI_NPI = "1497941645"
CASE = {"case_id": "MTS-0013", "policy_id": "ncd_100_1_bariatric", "pend_reason": "missing_documentation",
        "missing_facts": ["prior_medical_treatment_failed"], "ordering_npi": BARI_NPI, "received_at": NOW}


def lambda_event(sqs, url, n=10):
    """Receive from the queue and wrap the messages exactly as the SQS -> Lambda event source does."""
    msgs = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=n).get("Messages", [])
    return {"Records": [{"messageId": m["MessageId"], "receiptHandle": m["ReceiptHandle"], "body": m["Body"],
                         "eventSource": "aws:sqs"} for m in msgs]}


def test_queued_case_is_worked_by_the_lambda_and_logged_in_dynamodb(stores, http):
    store, log = stores
    sqs = boto3.client("sqs")
    q = create_queues("journey-intake", sqs)
    JourneyQueue(q["queue_url"], sqs).submit(CASE)
    assert JourneyQueue(q["queue_url"], sqs).depth()["waiting"] == 1

    out = process(lambda_event(sqs, q["queue_url"])["Records"], store, JourneyAgent(store, log, http=http, clock=lambda: NOW))
    assert out == {"batchItemFailures": [], "processed": ["MTS-0013"], "skipped": []}
    assert [t["tool"] for t in log.tasks("MTS-0013")][:3] == ["verify_provider", "coverage_policy", "request_records"]
    assert store.get("MTS-0013")["status"] != "new"


def test_redelivered_message_never_runs_the_agent_twice(stores, http):
    store, log = stores
    sqs = boto3.client("sqs")
    url = create_queues("journey-intake", sqs)["queue_url"]
    queue = JourneyQueue(url, sqs)
    queue.submit(CASE)
    queue.submit(CASE)  # at-least-once delivery: the same case can arrive twice
    out = process(lambda_event(sqs, url)["Records"], store, JourneyAgent(store, log, http=http, clock=lambda: NOW))
    assert out["processed"] == ["MTS-0013"] and out["skipped"] == ["MTS-0013"]
    assert len([t for t in log.tasks("MTS-0013") if t["tool"] == "request_records"]) == 1


def test_bad_message_fails_alone_and_reaches_the_dead_letter_queue(stores, http):
    store, log = stores
    sqs = boto3.client("sqs")
    q = create_queues("journey-intake", sqs)
    queue = JourneyQueue(q["queue_url"], sqs)
    queue.submit(CASE)
    sqs.send_message(QueueUrl=q["queue_url"], MessageBody=json.dumps({"case_id": "BROKEN"}))
    agent = JourneyAgent(store, log, http=http, clock=lambda: NOW)

    event = lambda_event(sqs, q["queue_url"])
    out = process(event["Records"], store, agent)
    assert out["processed"] == ["MTS-0013"] and len(out["batchItemFailures"]) == 1
    # the event source deletes successes; the failed one is retried until maxReceiveCount, then dead-lettered
    for rec in event["Records"]:
        if {"itemIdentifier": rec["messageId"]} in out["batchItemFailures"]:  # retry now instead of after 180 s
            sqs.change_message_visibility(QueueUrl=q["queue_url"], ReceiptHandle=rec["receiptHandle"], VisibilityTimeout=0)
        else:
            sqs.delete_message(QueueUrl=q["queue_url"], ReceiptHandle=rec["receiptHandle"])
    for _ in range(4):  # each failed retry counts as a receive; after the 3rd the queue moves it to the DLQ
        for m in sqs.receive_message(QueueUrl=q["queue_url"], MaxNumberOfMessages=10).get("Messages", []):
            assert process([{"messageId": m["MessageId"], "body": m["Body"]}], store, agent)["batchItemFailures"]
            sqs.change_message_visibility(QueueUrl=q["queue_url"], ReceiptHandle=m["ReceiptHandle"], VisibilityTimeout=0)
    dead = sqs.receive_message(QueueUrl=q["dlq_url"], MaxNumberOfMessages=10).get("Messages", [])
    assert [json.loads(m["Body"])["case_id"] for m in dead] == ["BROKEN"]


def test_lambda_handler_returns_the_partial_batch_response(stores, http, monkeypatch):
    store, log = stores
    monkeypatch.setattr(worker, "_deps", lambda: (store, JourneyAgent(store, log, http=http, clock=lambda: NOW)))
    resp = worker.handler({"Records": [{"messageId": "m1", "body": "not json"}]})
    assert resp == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
