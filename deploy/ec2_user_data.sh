#!/bin/bash
# EC2 user data (Amazon Linux 2023): runs the API and PostgreSQL in Docker on the instance.
# Attach an instance role that can sqs:SendMessage to journey-intake and read/write the journey-tasks table,
# then set the two values below from the SAM stack outputs.
set -euo pipefail
JOURNEY_SQS_URL="REPLACE_WITH_QueueUrl_OUTPUT"
JOURNEY_DYNAMO_TABLE="journey-tasks"
PG_PASSWORD="$(openssl rand -hex 16)"  # generated on the instance, never committed

dnf install -y docker git
systemctl enable --now docker
mkdir -p /usr/local/lib/docker/cli-plugins
curl -sSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

git clone https://github.com/achi-vyshnavi28/care-journey-agent.git /opt/care-journey-agent
cd /opt/care-journey-agent
cat > .env <<ENV
JOURNEY_SQS_URL=${JOURNEY_SQS_URL}
JOURNEY_DYNAMO_TABLE=${JOURNEY_DYNAMO_TABLE}
PG_PASSWORD=${PG_PASSWORD}
AWS_DEFAULT_REGION=$(curl -s http://169.254.169.254/latest/meta-data/placement/region)
ENV
chmod 600 .env
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
