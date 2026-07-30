from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pdf2zh_next.gptaction_api import GPTActionAPISettings
from pdf2zh_next.gptaction_api import create_app
from pdf2zh_next.gptaction_api import load_api_settings
from pdf2zh_next.gptaction_api import main
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
        max_requests=2,
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
    assert (
        schema["paths"]["/v1/actions/batches/next"]["post"]["x-openai-isConsequential"]
        is False
    )
    assert (
        schema["paths"]["/v1/actions/batches/submit"]["post"][
            "x-openai-isConsequential"
        ]
        is False
    )


def test_oversized_item_does_not_rollback_valid_result(tmp_path) -> None:
    database_path = tmp_path / "queue.sqlite3"
    settings = GPTActionAPISettings(
        api_key=API_KEY,
        queue_db=str(database_path),
        max_requests=2,
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


def test_api_settings_priority_explicit_env_dotenv_defaults(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "GPT_ACTION_API_KEY=dotenv-api-key-123456789",
                "GPT_ACTION_QUEUE_DB=./data/dotenv.sqlite3",
                "GPT_ACTION_API_PORT=8100",
                "GPT_ACTION_MAX_REQUESTS=4",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GPT_ACTION_API_PORT", "8200")
    monkeypatch.setenv("GPT_ACTION_MAX_REQUESTS", "6")

    settings = load_api_settings(api_port=8300)

    assert settings.api_key == "dotenv-api-key-123456789"
    assert settings.api_port == 8300
    assert settings.max_requests == 6
    assert settings.queue_db == str((tmp_path / "data/dotenv.sqlite3").resolve())
    assert settings.api_host == "127.0.0.1"


def test_api_cli_overrides_system_env_and_dotenv(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "GPT_ACTION_API_KEY=dotenv-api-key-123456789",
                "GPT_ACTION_API_PORT=8100",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GPT_ACTION_API_PORT", "8200")
    captured = {}

    def fake_run(app, **kwargs):
        captured["settings"] = app.state.settings
        captured["kwargs"] = kwargs

    monkeypatch.setattr("pdf2zh_next.gptaction_api.uvicorn.run", fake_run)

    main(["--api-port", "8300", "--api-host", "127.0.0.2"])

    assert captured["settings"].api_port == 8300
    assert captured["settings"].api_host == "127.0.0.2"
    assert captured["kwargs"]["port"] == 8300
    assert captured["kwargs"]["host"] == "127.0.0.2"


def test_default_claim_size_allows_four_sessions_to_share_eight_requests(
    tmp_path,
) -> None:
    database_path = tmp_path / "queue.sqlite3"
    settings = GPTActionAPISettings(api_key=API_KEY, queue_db=str(database_path))
    client = TestClient(create_app(settings))
    queue = GPTActionQueue(database_path)
    run_id = queue.start_run()
    for index in range(8):
        enqueue(queue, run_id, f"Request {index}")

    batches = [
        client.post("/v1/actions/batches/next", headers=AUTH, json={}).json()
        for _ in range(4)
    ]

    assert [len(batch["requests"]) for batch in batches] == [2, 2, 2, 2]
    request_ids = {
        item["request_id"] for batch in batches for item in batch["requests"]
    }
    assert len(request_ids) == 8


def test_public_example_api_key_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="public .env.example placeholder"):
        GPTActionAPISettings(
            api_key="replace-with-a-long-random-key",
            queue_db=str(tmp_path / "queue.sqlite3"),
        )


@pytest.mark.parametrize(
    "field_name",
    ["max_serialized_response_chars", "max_submit_chars", "max_output_chars"],
)
def test_action_character_limits_must_remain_below_100000(field_name, tmp_path) -> None:
    accepted = GPTActionAPISettings(
        api_key=API_KEY,
        queue_db=str(tmp_path / "accepted.sqlite3"),
        **{field_name: 99999},
    )
    assert getattr(accepted, field_name) == 99999

    with pytest.raises(ValueError, match="between 1000 and 99999"):
        GPTActionAPISettings(
            api_key=API_KEY,
            queue_db=str(tmp_path / "rejected.sqlite3"),
            **{field_name: 100000},
        )


def test_api_settings_use_pdf2zh_env_file_from_other_working_directory(
    monkeypatch, tmp_path
) -> None:
    config_dir = tmp_path / "config"
    launch_dir = tmp_path / "launch"
    config_dir.mkdir()
    launch_dir.mkdir()
    env_file = config_dir / ".env"
    env_file.write_text(
        "\n".join(
            [
                "GPT_ACTION_API_KEY=external-env-file-key-123456",
                "GPT_ACTION_QUEUE_DB=./data/shared.sqlite3",
                "GPT_ACTION_MAX_REQUESTS=2",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(launch_dir)
    monkeypatch.setenv("PDF2ZH_ENV_FILE", str(env_file))

    settings = load_api_settings()

    assert settings.api_key == "external-env-file-key-123456"
    assert settings.max_requests == 2
    assert settings.queue_db == str((config_dir / "data/shared.sqlite3").resolve())
