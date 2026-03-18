#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ "$(id -u)" = "0" ]; then
    echo "❌ Do not run this script as root. Please switch to the service user:"
    echo "   sudo su - tgbridge"
    exit 1
fi

export UID=$(id -u)
export GID=$(id -g)

git pull
docker compose up -d --build
docker image prune -f

echo "✅ Updated, restarted, and cleaned up old images."
