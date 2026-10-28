.PHONY: docker-open docker-compose docker-compose-down docker-compose-reset dev

docker-open:
	open -a Docker
	@until docker info >/dev/null 2>&1; do echo "Docker 시작 대기 중..."; sleep 2; done

docker-compose: docker-open
	docker compose up -d --wait

docker-compose-down:
	docker compose down

docker-compose-reset:
	docker compose down -v && docker compose up -d --wait

dev: docker-compose
	uv run uvicorn app.main:app --reload
