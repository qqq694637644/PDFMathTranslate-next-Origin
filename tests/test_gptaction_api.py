from __future__ import annotations

from fastapi.testclient import TestClient
from pdf2zh_next.gptaction_api import GPTActionAPISettings
from pdf2zh_next.gptaction_api import create_app
from pdf2zh_next.translator.gptaction_queue import GPTActionQueue

API_KEY = "personal-test-api-key-123456"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


def enqueue(queue: GPTActionQueue, run_id: str, text: str):
    return queue.enqueue(
        run_id=run_id,
        protocol_version="1",
        mode="SIMPLE_TEXT",
        lang_in="en",
        lang_out="zh",
        input_text=text,
        semantic_context={},
    )


def build_client(tmp_path):
    database_path = tmp_path / "queue.sqlite3"
    settings = GPTActionAPISettings(
        api_key=API_KEY,
        queue_db=str(database_path),
        max_requests=8,
        max_serialized_response_chars=30000,
        max_submit_chars=60000,
    )
    app = create_app(settings)
    return TestClient(app), GPTActionQueue(database_path)


def test_bearer_auth_and_queue_status(tmp_path) -> None:
    client, queue = build_client(tmp_path)
    assert client.get("/v1/actions/queue/status").status_code == 401

    idle = client.get("/v1/actions/queue/status", headers=AUTH)
    assert idle.status_code == 200
    assert idle.json()["status"] == "IDLE"

    run_id = queue.start_run()
    enqueue(queue, run_id, "Hello")
    active = client.get("/v1/actions/queue/status", headers=AUTH)
    assert active.status_code == 200
    assert active.json() == {
        "run_id": run_id,
        "status": "TRANSLATING",
        "pending": 1,
        "claimed": 0,
        "completed": 0,
        "run_active": True,
    }


def test_claim_and_partial_submit(tmp_path) -> None:
    client, queue = build_client(tmp_path)
    run_id = queue.start_run()
    enqueue(queue, run_id, "First")
    enqueue(queue, run_id, "Second")

    response = client.post(
        "/v1/actions/batches/next",
        headers=AUTH,
        json={"max_requests": 2},
    )
    assert response.status_code == 200
    batch = response.json()
    assert batch["run_id"] == run_id
    assert len(batch["requests"]) == 2

    first, second = batch["requests"]
    submitted = client.post(
        "/v1/actions/batches/submit",
        headers=AUTH,
        json={
            "results": [
                {
                    "request_id": first["request_id"],
                    "claim_token": first["claim_token"],
                    "output": "第一",
                },
                {
                    "request_id": second["request_id"],
                    "claim_token": "wrong-token",
                    "output": "第二",
                },
            ]
        },
    )
    assert submitted.status_code == 200
    assert submitted.json()["results"] == [
        {
            "request_id": first["request_id"],
            "status": "COMPLETED",
            "error": None,
        },
        {
            "request_id": second["request_id"],
            "status": "ERROR",
            "error": "claim_token does not match request_id",
        },
    ]

    status = client.get("/v1/actions/queue/status", headers=AUTH).json()
    assert status["completed"] == 1
    assert status["claimed"] == 1


def test_openapi_exposes_only_three_action_operations(tmp_path) -> None:
    client, _ = build_client(tmp_path)
    schema = client.get("/openapi.json").json()
    operation_ids = {
        operation["operationId"]
        for path in schema["paths"].values()
        for operation in path.values()
    }
    assert operation_ids == {"getQueueStatus", "getNextBatch", "submitBatch"}
    assert "HTTPBearer" in schema["components"]["securitySchemes"]


def test_oversized_item_does_not_rollback_valid_result(tmp_path) -> None:
    database_path = tmp_path / "queue.sqlite3"
    settings = GPTActionAPISettings(
        api_key=API_KEY,
        queue_db=str(database_path),
        max_requests=8,
        max_serialized_response_chars=30000,
        max_submit_chars=60000,
        max_output_chars=1000,
    )
    client = TestClient(create_app(settings))
    queue = GPTActionQueue(database_path)
    run_id = queue.start_run()
    enqueue(queue, run_id, "Valid")
    enqueue(queue, run_id, "Oversized")
    items = client.post(
        "/v1/actions/batches/next",
        headers=AUTH,
        json={"max_requests": 2},
    ).json()["requests"]

    response = client.post(
        "/v1/actions/batches/submit",
        headers=AUTH,
        json={
            "results": [
                {
                    "request_id": items[0]["request_id"],
                    "claim_token": items[0]["claim_token"],
                    "output": "有效结果",
                },
                {
                    "request_id": items[1]["request_id"],
                    "claim_token": items[1]["claim_token"],
                    "output": "x" * 1001,
                },
            ]
        },
    )

    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["status"] == "COMPLETED"
    assert results[1]["status"] == "ERROR"
    assert "per-item limit" in results[1]["error"]


def test_get_next_batch_reports_required_size_for_existing_request(tmp_path) -> None:
    database_path = tmp_path / "queue.sqlite3"
    queue = GPTActionQueue(database_path)
    run_id = queue.start_run()
    queue.enqueue(
        run_id=run_id,
        protocol_version="1",
        mode="SIMPLE_TEXT",
        lang_in="en",
        lang_out="zh",
        input_text="x" * 2000,
        semantic_context={},
        max_serialized_response_chars=5000,
    )
    client = TestClient(
        create_app(
            GPTActionAPISettings(
                api_key=API_KEY,
                queue_db=str(database_path),
                max_serialized_response_chars=1000,
            )
        )
    )

    response = client.post(
        "/v1/actions/batches/next",
        headers=AUTH,
        json={"max_requests": 1},
    )

    assert response.status_code == 413
    detail = response.json()["detail"]
    assert "required_chars=" in detail
    assert "max_chars=1000" in detail
