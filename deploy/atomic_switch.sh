#!/usr/bin/env bash
set -euo pipefail

RELEASE_DIR=${1:?release directory is required}
CURRENT_LINK=${2:-/opt/bot/813Assistant-current}
SERVICE_NAME=${3:-813assistant}
HEALTH_SECONDS=${4:-15}

case "$RELEASE_DIR" in
    /opt/bot/813Assistant-releases/*) ;;
    *) echo "Refusing release outside /opt/bot/813Assistant-releases" >&2; exit 2 ;;
esac

if [ ! -f "$RELEASE_DIR/bot/main.py" ] || [ ! -x "$RELEASE_DIR/.venv/bin/python" ]; then
    echo "Release is incomplete: $RELEASE_DIR" >&2
    exit 2
fi

PREVIOUS_TARGET=""
if [ -L "$CURRENT_LINK" ]; then
    PREVIOUS_TARGET=$(readlink -f "$CURRENT_LINK")
fi
NEXT_LINK="${CURRENT_LINK}.next.$$"
trap 'rm -f "$NEXT_LINK"' EXIT

ln -s "$RELEASE_DIR" "$NEXT_LINK"
mv -Tf "$NEXT_LINK" "$CURRENT_LINK"

rollback() {
    echo "New release did not stay healthy; rolling back" >&2
    if [ -n "$PREVIOUS_TARGET" ] && [ -d "$PREVIOUS_TARGET" ]; then
        ln -s "$PREVIOUS_TARGET" "$NEXT_LINK"
        mv -Tf "$NEXT_LINK" "$CURRENT_LINK"
        systemctl restart "$SERVICE_NAME"
    fi
}

systemctl daemon-reload
if ! systemctl restart "$SERVICE_NAME"; then
    rollback
    exit 1
fi

for _second in $(seq 1 "$HEALTH_SECONDS"); do
    sleep 1
    if ! systemctl is-active --quiet "$SERVICE_NAME"; then
        rollback
        exit 1
    fi
done

echo "Release active: $RELEASE_DIR"
