base_url := env_var_or_default("BASE_URL", "http://localhost:8000/api/v1")
workers := env_var_or_default("WORKERS", "3")
run_k6 := env_var_or_default("RUN_K6", "0")
k6_mode := env_var_or_default("K6_MODE", "native")

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

pre-release:
    BASE_URL={{base_url}} WORKERS={{workers}} RUN_K6={{run_k6}} K6_MODE={{k6_mode}} bash scripts/pre_release_check.sh
