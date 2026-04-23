.PHONY: help build up down restart logs logs-worker logs-api ps sh-worker sh-api shell stats sync clean wipe

# Default to `op run` so 1Password resolves op:// refs in .env on the host.
# Override with `OP= make up` if you want to pass plain values yourself.
OP ?= op run --env-file .env --

help:
	@echo "utalkn2me — UniFi Talk scraper + transcriber + REST API"
	@echo
	@echo "  make build       build both images"
	@echo "  make up          build + start worker and api (detached)"
	@echo "  make down        stop and remove containers"
	@echo "  make restart     down + up"
	@echo "  make logs        follow logs from both services"
	@echo "  make logs-worker follow worker logs only"
	@echo "  make logs-api    follow api logs only"
	@echo "  make ps          show container status"
	@echo "  make sh-worker   shell into the worker container"
	@echo "  make sh-api      shell into the api container"
	@echo "  make stats       GET /stats from the running API"
	@echo "  make sync        run one sync cycle in a throwaway container"
	@echo "  make wipe        DANGER: rm -rf ./data (drops DB, recordings)"

build:
	$(OP) docker compose build

up:
	$(OP) docker compose up -d --build

down:
	docker compose down

restart: down up

logs:
	docker compose logs -f

logs-worker:
	docker compose logs -f worker

logs-api:
	docker compose logs -f api

ps:
	docker compose ps

sh-worker:
	docker compose exec worker /bin/bash

sh-api:
	docker compose exec api /bin/bash

stats:
	@curl -s http://127.0.0.1:$${API_PORT:-8000}/stats | python3 -m json.tool

sync:
	$(OP) docker compose run --rm worker sync --transcribe \
		--db /data/calls.db --recordings-dir /data/recordings

wipe:
	@echo "This will delete ./data (DB, recordings, session, transcripts cache)."
	@read -p "Type 'yes' to confirm: " ans; [ "$$ans" = "yes" ] && rm -rf data || echo "aborted"
