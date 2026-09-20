#!/bin/bash
set -eu
cd /home/bracketbot/act-local
deadline=$((SECONDS + 1800))
until test -f prepared/manifest.json; do
    if ! pgrep -f '^.venv/bin/python prepare.py$' >/dev/null; then
        echo 'Preparation exited without a manifest; inspect prepare.log.'
        exit 1
    fi
    if (( SECONDS >= deadline )); then
        echo 'Preparation exceeded 30 minute deadline.'
        exit 1
    fi
    sleep 10
done
while pgrep -f '^.venv/bin/python benchmark.py --real$' >/dev/null; do sleep 5; done
exec .venv/bin/python -u train.py --steps 2000
