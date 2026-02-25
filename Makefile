.PHONY: load-chat load-telegram-soak multi-up multi-check smoke-all

BASE_URL ?= http://localhost:8000/api/v1
WORKERS ?= 3
K6 ?= k6
DOCKER_K6_IMAGE ?= grafana/k6

load-chat:
	$(K6) run -e BASE_URL=$(BASE_URL) scripts/load/k6_chat_worker_burst.js

load-telegram-soak:
	$(K6) run -e BASE_URL=$(BASE_URL) scripts/load/k6_telegram_polling_soak.js

multi-up:
	docker compose -f docker-compose.yml -f docker-compose.multi.yml --profile multi up -d --build --scale worker=$(WORKERS)

multi-check:
	bash deploy/check-multi.sh $(WORKERS)

smoke-all:
	python -m scripts.smoke_all
