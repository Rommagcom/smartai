base_url := env_var_or_default("BASE_URL", "http://localhost:8000/api/v1")
workers := env_var_or_default("WORKERS", "3")

load-chat:
    k6 run -e BASE_URL={{base_url}} scripts/load/k6_chat_worker_burst.js

load-telegram-soak:
    k6 run -e BASE_URL={{base_url}} scripts/load/k6_telegram_polling_soak.js

multi-up:
    docker compose -f docker-compose.yml -f docker-compose.multi.yml --profile multi up -d --build --scale worker={{workers}}

multi-check:
    bash deploy/check-multi.sh {{workers}}

smoke-all:
    python -m scripts.smoke_all
