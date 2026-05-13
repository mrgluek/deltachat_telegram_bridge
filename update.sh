#!/bin/bash
set -e
cd "$(dirname "$0")"

# --- HEALTHCHECKS SUPPORT ---
# Create a .env.local file in this directory to enable monitoring:
#   echo 'MONITOR_URL=https://ping.gluek.info/ping/YOUR-UUID' > .env.local
[ -f .env.local ] && . .env.local

hc_ping() {
    [ -n "$MONITOR_URL" ] && curl -fsS -m 10 --retry 5 -o /dev/null "${MONITOR_URL}${1:-}" || true
}
trap 'hc_ping /fail' ERR
hc_ping /start

if [ "$(id -u)" = "0" ]; then
    echo "❌ Do not run this script as root. Please switch to the service user:"
    echo "   sudo su - tgbridge"
    exit 1
fi

# Automatically create/update .env file with host user IDs
echo "HOST_UID=$(id -u)" > .env
echo "HOST_GID=$(id -g)" >> .env

export HOST_UID=$(id -u)
export HOST_GID=$(id -g)

# --- BACKUP REMOTE ---
BACKUP_REMOTE_URL="https://git.gluek.info/gluek/deltachat_telegram_bridge"

if ! git remote get-url backup &>/dev/null; then
    echo "➕ Adding Forgejo mirror as 'backup' remote..."
    git remote add backup "$BACKUP_REMOTE_URL"
fi

echo "Checking for updates..."

# --- FALLBACK LOGIC ---
ACTIVE_REMOTE=""

if git fetch origin; then
    ACTIVE_REMOTE="origin"
    echo "✅ GitHub (origin) is reachable."
elif git fetch backup; then
    ACTIVE_REMOTE="backup"
    echo "⚠️ GitHub unreachable. Using Forgejo (backup) instead."
else
    echo "❌ ERROR: Both GitHub and Forgejo are unreachable!"
    exit 1
fi

# --- BRANCH DETECTION ---
REMOTE_REF=$(git branch -r | grep "^  $ACTIVE_REMOTE/" | grep -v "HEAD" | head -n 1 | sed 's/^[[:space:]]*//')

if [ -z "$REMOTE_REF" ]; then
    echo "❌ ERROR: Could not detect a valid remote branch for $ACTIVE_REMOTE."
    exit 1
fi

REMOTE=$(git rev-parse $REMOTE_REF)
BRANCH_NAME=${REMOTE_REF#$ACTIVE_REMOTE/}

LOCAL=$(git rev-parse HEAD)

FORCE=false
if [ "$1" = "-f" ] || [ "$1" = "--force" ]; then
    FORCE=true
fi

if [ "$LOCAL" != "$REMOTE" ] || [ "$FORCE" = true ]; then
    if [ "$FORCE" = true ]; then
        echo "🔄 Force update requested."
    else
        echo "🆕 New changes detected on $ACTIVE_REMOTE ($BRANCH_NAME). Updating..."
        git pull $ACTIVE_REMOTE $BRANCH_NAME
    fi
    docker compose up -d --build
    docker image prune -f
    echo "✅ Updated, restarted, and cleaned up old images."
else
    echo "✅ Already up to date (via $ACTIVE_REMOTE). Use -f to force rebuild."
fi

hc_ping
