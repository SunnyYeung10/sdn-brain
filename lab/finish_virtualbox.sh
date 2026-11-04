#!/usr/bin/env bash
set -Eeuo pipefail
SOURCE=/mnt/codex-sdn
STATUS=/mnt/codex-sdn-status
test "$(id -u)" = 0
project=$(cat "$STATUS/project-path.txt")
case "$project" in /home/sdn/sdn-brain-20261102-*) ;; *) exit 1 ;; esac
test -d "$project/.venv"
exec > >(tee -a "$STATUS/finish.log") 2>&1
trap 'rc=$?; printf "validation failed (line %s, exit %s)\n" "$LINENO" "$rc" > "$STATUS/status.txt"; exit "$rc"' ERR
cp -a "$SOURCE/web"/. "$project/web/"
for name in traffic.py validate_virtualbox.py load_validate_virtualbox.py; do
    cp "$SOURCE/lab/$name" "$project/lab/$name"
done
cp -a "$SOURCE/tests"/. "$project/tests/"
chown -R sdn:sdn "$project/web" "$project/lab" "$project/tests"
cd "$project"
.venv/bin/python -m unittest discover -s tests -v
for attempt in 1 2 3; do
    for name in traffic.py validate_virtualbox.py load_validate_virtualbox.py; do
        cp "$SOURCE/lab/$name" "$project/lab/$name"
    done
    cp -a "$SOURCE/web"/. "$project/web/"
    printf 'actual traffic validation attempt %s\n' "$attempt" > "$STATUS/status.txt"
    if .venv/bin/python lab/validate_virtualbox.py --status "$STATUS" &&
       .venv/bin/python -m lab.load_validate_virtualbox --status "$STATUS"; then
        printf 'ready\n' > "$STATUS/status.txt"
        exit 0
    fi
    printf 'validation failed on attempt %s; waiting 30 seconds before bounded retry\n' "$attempt"
    sleep 30
done
printf 'validation failed after three attempts\n' > "$STATUS/status.txt"
exit 1
