# Deploying on AWS

| Piece | AWS service | File |
|---|---|---|
| API + PostgreSQL (Docker) | EC2 (Amazon Linux 2023) | `ec2_user_data.sh` |
| Asynchronous intake | SQS queue + dead-letter queue | `template.yaml` |
| Agent worker | Lambda (SQS event source, partial batch responses) | `template.yaml`, `journey/worker.py` |
| Agent task log, follow-ups | DynamoDB (single table, GSI) | `template.yaml` |

1. `sam build && sam deploy --guided` (from `deploy/`), giving the PostgreSQL URL of the EC2 host as `DbUrl`.
2. Launch an EC2 instance with `ec2_user_data.sh` as user data, after pasting the `QueueUrl` output into it.
3. `POST /journeys/queue` on the instance puts a case on SQS; the Lambda works it and writes the trace to DynamoDB.

Locally and in CI the same code runs on moto (`tests/test_queue.py`), so no AWS account is needed for the tests.
