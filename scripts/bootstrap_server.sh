#!/usr/bin/env sh
# New server: Schools, rubric and test users of every role (idempotent, safe to run again).
#   ./scripts/bootstrap_server.sh                 # password from TEST_USERS_PASSWORD in .env, else asked for
#   ./scripts/bootstrap_server.sh --allow-prod    # with ENV=prod
# Run from syllabus-ai-backend/ after `docker compose up -d`.
set -eu
cd "$(dirname "$0")/.."
if grep -Eq '^TEST_USERS_PASSWORD=.+' .env 2>/dev/null; then
  exec docker compose exec api python -m app.cli bootstrap "$@"
fi
exec docker compose exec api python -m app.cli bootstrap --password "$@"
