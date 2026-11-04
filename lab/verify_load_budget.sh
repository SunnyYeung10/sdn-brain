#!/usr/bin/env bash
set -Eeuo pipefail
SOURCE=/mnt/codex-sdn
STATUS=/mnt/codex-sdn-status
test "$(id -u)" = 0
project=$(cat "$STATUS/project-path.txt")
case "$project" in /home/sdn/sdn-brain-20261102-*) ;; *) exit 1 ;; esac
exec > >(tee -a "$STATUS/load-budget.log") 2>&1
trap 'rc=$?; printf "load-budget validation failed (line %s, exit %s)\n" "$LINENO" "$rc" > "$STATUS/status.txt"; exit "$rc"' ERR
cp "$SOURCE/lab/load_validate_virtualbox.py" "$project/lab/load_validate_virtualbox.py"
chown sdn:sdn "$project/lab/load_validate_virtualbox.py"
cd "$project"
printf 'testing measured load against a temporary 1 Mbps controller budget\n' > "$STATUS/status.txt"
.venv/bin/python -m lab.load_validate_virtualbox --status "$STATUS"
printf 'ready\n' > "$STATUS/status.txt"
