#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ "$(id -u)" = "0" ]; then
    echo "❌ Do not run this script as root. Please switch to the service user:"
    echo "   sudo su - tgbridge"
    exit 1
fi

git pull
docker compose up -d --build

echo "✅ Updated and restarted."
