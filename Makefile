.PHONY: help build up up-api down down-api restart logs logs-worker logs-api logs-pusher ps sh-worker sh-api sh-pusher stats sync last wipe

# Default to `op run` so 1Password resolves op:// refs in .env on the host.
# Override with `OP= make up` if you want to pass plain values yourself.
OP ?= op run --env-file .env --

help:
	@echo "utalkn2me — UniFi Talk scraper + transcriber + webhook pusher"
	@echo
	@echo "  make build       build images"
	@echo "  make up          build + start worker + pusher (API off)"
	@echo "  make up-api      build + start worker + pusher + local read-only API"
	@echo "  make down        stop and remove all containers"
	@echo "  make down-api    stop just the API container (worker + pusher keep running)"
	@echo "  make restart     down + up"
	@echo "  make logs        follow logs from all running services"
	@echo "  make logs-worker follow worker logs"
	@echo "  make logs-pusher follow pusher logs"
	@echo "  make logs-api    follow api logs (when running with --profile api)"
	@echo "  make ps          show container status"
	@echo "  make sh-worker   shell into the worker container"
	@echo "  make sh-pusher   shell into the pusher container"
	@echo "  make sh-api      shell into the api container"
	@echo "  make stats       GET /stats from the running API"
	@echo "  make last        show the most recent call + transcript via API"
	@echo "  make sync        run one sync cycle in a throwaway container"
	@echo "  make wipe        DANGER: rm -rf ./data (drops DB, recordings)"

build:
	$(OP) docker compose build

up:
	$(OP) docker compose up -d --build

up-api:
	$(OP) docker compose --profile api up -d --build

down:
	docker compose --profile api down

down-api:
	docker compose stop api && docker compose rm -f api

restart: down up

logs:
	docker compose logs -f

logs-worker:
	docker compose logs -f worker

logs-pusher:
	docker compose logs -f pusher

logs-api:
	docker compose logs -f api

ps:
	docker compose --profile api ps

sh-worker:
	docker compose exec worker /bin/bash

sh-pusher:
	docker compose exec pusher /bin/bash

sh-api:
	docker compose exec api /bin/bash

stats:
	@curl -s http://127.0.0.1:$${API_PORT:-8000}/stats | python3 -m json.tool

last:
	@curl -s 'http://127.0.0.1:$${API_PORT:-8000}/calls?limit=1' | python3 -m json.tool

sync:
	$(OP) docker compose run --rm worker sync --transcribe \
		--db /data/calls.db --recordings-dir /data/recordings

wipe:
	@echo "This will delete ./data (DB, recordings, session, transcripts cache)."
	@read -p "Type 'yes' to confirm: " ans; [ "$$ans" = "yes" ] && rm -rf data || echo "aborted"
