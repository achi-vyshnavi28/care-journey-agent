"""Asynchronous intake on AWS: pended cases go onto an SQS queue, and a Lambda worker runs the journey agent on them.

    API (EC2)  --POST /journeys/queue-->  SQS journey-intake  --event source-->  Lambda worker.handler
                                              | after 3 failed receives
                                              v
                                          SQS journey-intake-dlq (kept 14 days for inspection)

The worker is idempotent (a case already in the store is skipped, so an SQS redelivery never runs the agent twice)
and reports partial batch failures, so one bad message is retried alone instead of failing the whole batch.
"""
import json
import os
from datetime import datetime, timezone

MAX_RECEIVES = 3


def create_queues(name, client) -> dict:
    """Create the intake queue and its dead-letter queue; returns their URLs."""
    dlq_url = client.create_queue(QueueName=f"{name}-dlq", Attributes={"MessageRetentionPeriod": str(14 * 24 * 3600)})["QueueUrl"]
    dlq_arn = client.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    url = client.create_queue(QueueName=name, Attributes={
        "VisibilityTimeout": "180",  # longer than the worker's timeout, so a running message is not handed out twice
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(MAX_RECEIVES)}),
    })["QueueUrl"]
    return {"queue_url": url, "dlq_url": dlq_url}


class JourneyQueue:
    def __init__(self, queue_url=None, client=None):
        if client is None:
            import boto3

            client = boto3.client("sqs")
        self.client, self.url = client, queue_url or os.environ["JOURNEY_SQS_URL"]

    def submit(self, case: dict) -> str:
        body = {**case, "received_at": (case.get("received_at") or datetime.now(timezone.utc)).isoformat()}
        return self.client.send_message(QueueUrl=self.url, MessageBody=json.dumps(body))["MessageId"]

    def depth(self) -> dict:
        a = self.client.get_queue_attributes(QueueUrl=self.url, AttributeNames=[
            "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        return {"waiting": int(a["ApproximateNumberOfMessages"]), "in_flight": int(a["ApproximateNumberOfMessagesNotVisible"])}


REQUIRED = ("case_id", "policy_id", "pend_reason", "ordering_npi")


def process(records: list[dict], store, agent) -> dict:
    """Run the agent on a batch of SQS records. Returns the Lambda partial-batch response plus a summary."""
    failures, done, skipped = [], [], []
    for rec in records:
        try:
            case = json.loads(rec["body"])
            missing = [k for k in REQUIRED if not case.get(k)]
            if missing:
                raise ValueError(f"message missing {missing}")
            if store.get(case["case_id"]):  # redelivered or duplicate: already handled
                skipped.append(case["case_id"])
                continue
            store.create({"journey_id": case["case_id"], "policy_id": case["policy_id"],
                          "urgency": case.get("urgency", "standard"),
                          "received_at": datetime.fromisoformat(case["received_at"]),
                          "pend_reason": case["pend_reason"], "missing_facts": case.get("missing_facts", [])})
            agent.run(case["case_id"], case["ordering_npi"])
            done.append(case["case_id"])
        except Exception:
            failures.append({"itemIdentifier": rec["messageId"]})
    return {"batchItemFailures": failures, "processed": done, "skipped": skipped}
