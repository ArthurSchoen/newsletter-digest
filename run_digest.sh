#!/bin/bash
# One-click shortcut to run the digest immediately, for local use.
# In production the job runs from .github/workflows/daily-digest.yml instead,
# where the four secrets come from the repository settings rather than a file.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "Missing .env file at $SCRIPT_DIR/.env"
  echo "It needs GENAI_API_KEY, SENDER_EMAIL, SENDER_APP_PASSWORD, RECEIVER_EMAIL."
  exit 1
fi

# Load env vars, ignoring comments and blank lines.
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

python3 "$SCRIPT_DIR/Newsletter_API.py"
