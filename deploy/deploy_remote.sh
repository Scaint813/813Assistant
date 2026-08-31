#!/usr/bin/env bash
set -euo pipefail

DEPLOY_HOST=${1:-813assistant}
STATE_DIR=/opt/bot/813Assistant
RELEASES_DIR=/opt/bot/813Assistant-releases
CURRENT_LINK=/opt/bot/813Assistant-current
SERVICE_NAME=813assistant
COMMIT=$(git rev-parse HEAD)
RELEASE_DIR="$RELEASES_DIR/$COMMIT"

if [ -n "$(git status --short)" ]; then
    echo "Refusing to deploy a dirty worktree" >&2
    exit 2
fi

ssh "$DEPLOY_HOST" "mkdir -p '$RELEASE_DIR' '$STATE_DIR/backups'"
git archive "$COMMIT" | ssh "$DEPLOY_HOST" "tar -xf - -C '$RELEASE_DIR'"

ssh "$DEPLOY_HOST" bash -s -- "$RELEASE_DIR" "$STATE_DIR" <<'REMOTE'
set -euo pipefail
RELEASE_DIR=$1
STATE_DIR=$2

ln -sfn "$STATE_DIR/.env" "$RELEASE_DIR/.env"
ln -sfn "$STATE_DIR/assistant.db" "$RELEASE_DIR/assistant.db"
ln -sfn "$STATE_DIR/backups" "$RELEASE_DIR/backups"

if [ ! -x "$RELEASE_DIR/.venv/bin/python" ]; then
    python3 -m venv "$RELEASE_DIR/.venv"
fi
"$RELEASE_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -q \
    -r "$RELEASE_DIR/requirements.txt"
cd "$RELEASE_DIR"
"$RELEASE_DIR/.venv/bin/python" -m compileall -q bot
"$RELEASE_DIR/.venv/bin/python" -m unittest discover -s tests

STAMP=$(date -u +%Y%m%d-%H%M%S)
BACKUP="$STATE_DIR/backups/pre-deploy-$STAMP.sqlite3"
"$RELEASE_DIR/.venv/bin/python" -c \
    'import sqlite3,sys; source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2]); source.backup(target); target.close(); source.close()' \
    "$STATE_DIR/assistant.db" "$BACKUP"
"$RELEASE_DIR/.venv/bin/python" "$RELEASE_DIR/deploy/verify_sqlite_backup.py" "$BACKUP"

STAGING_DB=$(mktemp /tmp/813assistant-staging-XXXXXX)
trap 'rm -f "$STAGING_DB"' EXIT
cp "$BACKUP" "$STAGING_DB"
"$RELEASE_DIR/.venv/bin/python" "$RELEASE_DIR/deploy/preflight_database.py" "$STAGING_DB"
REMOTE

ssh "$DEPLOY_HOST" bash -s -- \
    "$RELEASE_DIR" "$STATE_DIR" "$CURRENT_LINK" "$SERVICE_NAME" <<'REMOTE'
set -euo pipefail
RELEASE_DIR=$1
STATE_DIR=$2
CURRENT_LINK=$3
SERVICE_NAME=$4

if [ ! -e "$CURRENT_LINK" ] && [ ! -L "$CURRENT_LINK" ]; then
    ln -s "$STATE_DIR" "$CURRENT_LINK"
fi
cp "$RELEASE_DIR/deploy/813assistant.service.example" \
    "/etc/systemd/system/$SERVICE_NAME.service"
chmod +x "$RELEASE_DIR/deploy/atomic_switch.sh"
"$RELEASE_DIR/deploy/atomic_switch.sh" \
    "$RELEASE_DIR" "$CURRENT_LINK" "$SERVICE_NAME" 15
REMOTE

ssh "$DEPLOY_HOST" \
    "systemctl is-active '$SERVICE_NAME' && readlink -f '$CURRENT_LINK' && journalctl -u '$SERVICE_NAME' -n 25 --no-pager"
