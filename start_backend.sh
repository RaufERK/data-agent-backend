#!/bin/bash
# Бэкенд: FastAPI на http://localhost:8000
cd "$(dirname "$0")"
if [ -f .env ]; then
    # export переменных из .env вручную, чтобы значения со скобками не ломались
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^#.*$ || -z "$line" ]] && continue
        export "$line"
    done < .env
fi
.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
