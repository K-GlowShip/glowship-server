import uuid

from httpx import ASGITransport, AsyncClient

from app.main import app


async def _post(body: dict):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/api/v1/dev/agent/dry-run", json=body)


async def test_dry_run은_빈_노드_그래프를_끝까지_실행한다():
    run_id = str(uuid.uuid4())

    response = await _post({"analysis_run_id": run_id})

    assert response.status_code == 200
    body = response.json()
    assert body["analysis_run_id"] == run_id
    assert body["outcome"] == "READY_FOR_REVIEW"
    assert body["last_step"] == "VALIDATING_OUTPUT"
    assert body["visited_nodes"][0] == "prepare"
    assert body["visited_nodes"][-1] == "validate"


async def test_dry_run은_입력이_없어도_임의_ID로_실행한다():
    response = await _post({})

    assert response.status_code == 200
    assert response.json()["outcome"] == "READY_FOR_REVIEW"


async def test_dry_run은_UUID_형식이_아니면_422를_반환한다():
    response = await _post({"analysis_run_id": "not-a-uuid"})

    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
