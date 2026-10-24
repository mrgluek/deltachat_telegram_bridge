#!/bin/bash
set -eE  # -E: the ERR trap below also fires inside main()
cd "$(dirname "$0")"

# Prevent git from hanging on interactive credentials prompt
export GIT_TERMINAL_PROMPT=0

# Deploys the newest release: the highest vX.Y.Z tag, not the tip of master, so
# only what was explicitly released reaches the server. In .env.local:
#   UPDATE_REF=v2.30.0   stay on that release (roll back; empty = newest again)
#   UPDATE_REF=master    follow a branch instead, as before releases
# A tag that was moved on the remote makes the fetch fail ("would clobber
# existing tag"): released versions do not change, so check before deleting
# the local tag.

# The whole script is one function: bash reads a script while running it, and
# the git reset below rewrites this file.
main() {

# --- HEALTHCHECKS SUPPORT ---
# Create a .env.local file in this directory to enable monitoring:
#   echo 'MONITOR_URL=https://ping.gluek.info/ping/YOUR-UUID' > .env.local
[ -f .env.local ] && . .env.local

hc_ping() {
    [ -n "$MONITOR_URL" ] && curl -fsS -m 10 --retry 5 -o /dev/null "${MONITOR_URL}${1:-}" || true
}
fail() {
    echo "❌ ERROR: $*"
    hc_ping /fail
    exit 1
}
trap 'hc_ping /fail' ERR
hc_ping /start

# --- BACKUP REMOTE ---
BACKUP_REMOTE_URL="https://git.gluek.info/gluek/deltachat_telegram_bridge"

if ! git remote get-url backup &>/dev/null; then
    echo "➕ Adding Forgejo mirror as 'backup' remote..."
    git remote add backup "$BACKUP_REMOTE_URL"
fi

echo "Checking for updates..."

# --- FALLBACK LOGIC ---
ACTIVE_REMOTE=""

if git fetch --tags origin; then
    ACTIVE_REMOTE="origin"
    echo "✅ GitHub (origin) is reachable."
elif git fetch --tags backup; then
    ACTIVE_REMOTE="backup"
    echo "⚠️ GitHub unreachable. Using Forgejo (backup) instead."
else
    fail "Both GitHub and Forgejo are unreachable (or a release tag was moved, see above)!"
fi

# --- WHAT TO DEPLOY ---
if [ -z "$UPDATE_REF" ]; then
    TAG=$(git tag -l 'v*' --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n 1 || true)
    if [ -z "$TAG" ]; then
        fail "No release tag (vX.Y.Z) found."
    fi
    TARGET="refs/tags/$TAG"
    WHAT="release $TAG"
elif git rev-parse --verify --quiet "refs/tags/$UPDATE_REF^{commit}" >/dev/null; then
    TARGET="refs/tags/$UPDATE_REF"
    WHAT="release $UPDATE_REF (pinned by UPDATE_REF)"
elif git rev-parse --verify --quiet "refs/remotes/$ACTIVE_REMOTE/$UPDATE_REF" >/dev/null; then
    TARGET="refs/remotes/$ACTIVE_REMOTE/$UPDATE_REF"
    WHAT="branch $UPDATE_REF on $ACTIVE_REMOTE (UPDATE_REF)"
else
    fail "UPDATE_REF=$UPDATE_REF is neither a tag nor a branch on $ACTIVE_REMOTE."
fi

REMOTE=$(git rev-parse "$TARGET^{commit}")
LOCAL=$(git rev-parse HEAD)

FORCE=false
if [ "$1" = "-f" ] || [ "$1" = "--force" ]; then
    FORCE=true
fi

if [ "$LOCAL" != "$REMOTE" ] || [ "$FORCE" = true ]; then
    if [ "$FORCE" = true ]; then
        echo "🔄 Force update requested."
    else
        echo "🆕 Deploying $WHAT..."
        git reset --hard "$REMOTE"
    fi
    docker compose up -d --build
    docker image prune -f
    echo "✅ Updated, restarted, and cleaned up old images: $(git describe --tags --always)."
else
    echo "✅ Already on $WHAT (via $ACTIVE_REMOTE). Use -f to force rebuild."
fi

hc_ping
}

main "$@"
exit
