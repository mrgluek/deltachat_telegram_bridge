#!/bin/bash
set -e
cd "$(dirname "$0")"

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

BACKUP_REMOTE_URL="https://git.gluek.info/gluek/deltachat_telegram_bridge"

# Ensure the backup remote is configured
if ! git remote get-url backup &>/dev/null; then
    echo "➕ Adding Forgejo mirror as 'backup' remote..."
    git remote add backup "$BACKUP_REMOTE_URL"
fi

echo "Checking for updates..."

# --- FALLBACK LOGIC ---
ACTIVE_REMOTE=""

# Try fetching from GitHub (origin)
if git fetch origin; then
    ACTIVE_REMOTE="origin"
    echo "✅ GitHub (origin) is reachable."
# If GitHub fails, try Forgejo (backup)
elif git fetch backup; then
    ACTIVE_REMOTE="backup"
    echo "⚠️ GitHub unreachable. Using Forgejo (backup) instead."
else
    echo "❌ ERROR: Both GitHub and Forgejo are unreachable!"
    exit 1
fi

# --- ROBUST BRANCH DETECTION LOGIC ---
REMOTE_REF=$(git branch -r | grep "^  $ACTIVE_REMOTE/" | grep -v "HEAD" | head -n 1 | sed 's/^[[:space:]]*//')

if [ -z "$REMOTE_REF" ]; then
    echo "❌ ERROR: Could not detect a valid remote branch for $ACTIVE_REMOTE."
    exit 1
fi

REMOTE=$(git rev-parse $REMOTE_REF)
BRANCH_NAME=$(git rev-parse --abbrev-ref $REMOTE_REF)

# -------------------------

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
