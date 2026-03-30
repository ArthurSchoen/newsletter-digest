#!/bin/bash
# One-click shortcut to run the digest immediately

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "❌ Missing .env file at $SCRIPT_DIR/.env"
  exit 1
fi

# Load env vars
export $(grep -v '^#' "$ENV_FILE" | xargs)

/Users/arthurschoen/cursor_env/bin/python3 "$SCRIPT_DIR/Newsletter_API.py"
