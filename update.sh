#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ "$(id -u)" = "0" ]; then
    echo "❌ Do not run this script as root. Please switch to the service user:"
    echo "   sudo su - tgbridge"
    exit 1
fi

# Automatically create/update .env file with host user IDs
# This is needed because UID is readonly in bash and won't be exported automatically to docker-compose
echo "HOST_UID=$(id -u)" > .env
echo "HOST_GID=$(id -g)" >> .env

export HOST_UID=$(id -u)
export HOST_GID=$(id -g)

echo "Checking for updates..."
git fetch

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse @{u})

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "🆕 New changes detected. Updating..."
    git pull
    docker compose up -d --build
    docker image prune -f
    echo "✅ Updated, restarted, and cleaned up old images."
else
    echo "✅ Already up to date. No rebuild needed."
fi
