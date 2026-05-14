#!/usr/bin/env bash
set -euo pipefail
if rg -n "^(<<<<<<<|=======|>>>>>>>)" README.md bot >/dev/null; then
  echo "Conflict markers found. Resolve merge conflicts before commit."
  exit 1
fi
echo "No conflict markers found."
