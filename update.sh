#!/bin/bash
set -e
cd "$(dirname "$0")"

# Detect the actual owner of this directory
OWNER_UID=$(stat -c '%u' .)
OWNER_GID=$(stat -c '%g' .)

if [ "$(id -u)" = "0" ] && [ "$OWNER_UID" != "0" ]; then
    echo "⚠️  Running as root, but repo is owned by UID $OWNER_UID. Fixing permissions after update."
    git pull
    docker compose up -d --build
    chown -R "$OWNER_UID:$OWNER_GID" ~/.config/tgbridge/ 2>/dev/null || true
else
    git pull
    docker compose up -d --build
fi

echo "✅ Updated and restarted."
