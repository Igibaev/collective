#!/usr/bin/env bash
# Запуск без Docker: ./run.sh  (нужен Python 3.10+)
set -e
cd "$(dirname "$0")"
[ -f .env ] || { echo "Создайте .env из .env.example"; exit 1; }
set -a; . ./.env; set +a
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
export DATA_DIR="${DATA_DIR:-./data}"
exec .venv/bin/gunicorn -w 1 --threads 4 -b "${HOST:-127.0.0.1}:${PORT:-8000}" --timeout 180 app:app
