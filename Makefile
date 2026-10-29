.PHONY: docker-open docker-compose docker-compose-down docker-compose-reset dev db-migrate db-seed db-reseed

# 합성데이터 폴더 (manifest.json 이 있는 폴더). 예: make db-seed SEED_DATA_DIR=~/Desktop/.../20261028T060456Z
SEED_DATA_DIR ?=

docker-open:
	open -a Docker
	@until docker info >/dev/null 2>&1; do echo "Docker 시작 대기 중..."; sleep 2; done

docker-compose: docker-open
	docker compose up -d --wait

docker-compose-down:
	docker compose down

docker-compose-reset:
	docker compose down -v && docker compose up -d --wait

dev: docker-compose db-migrate
	uv run uvicorn app.main:app --reload

db-migrate:
	uv run alembic upgrade head

db-seed: db-migrate
	uv run python -m scripts.seed_synthetic_data "$(SEED_DATA_DIR)"

db-reseed: db-migrate
	uv run python -m scripts.seed_synthetic_data "$(SEED_DATA_DIR)" --reset
