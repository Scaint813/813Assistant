#!/usr/bin/env bash
set -e

pattern_start='<<<<''<<<'
pattern_mid='======='
pattern_end='>>>>''>>>'

if grep -R "${pattern_start}\|${pattern_mid}\|${pattern_end}" . --exclude-dir=.git --exclude=scripts_check_no_conflicts.sh; then
  echo "ERROR: Git conflict markers found"
  exit 1
fi

python -m compileall .

echo "OK: no conflict markers and Python syntax is valid"
