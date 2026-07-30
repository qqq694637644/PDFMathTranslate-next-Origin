from __future__ import annotations

import json

from pdf2zh_next.gptaction_queue_cli import GPTActionQueueCLISettings
from pdf2zh_next.gptaction_queue_cli import load_queue_cli_settings
from pdf2zh_next.gptaction_queue_cli import main
from pdf2zh_next.translator.gptaction_queue import GPTActionQueue


def test_recover_active_run_command_requires_matching_confirmation(
    monkeypatch, capsys, tmp_path
) -> None:
    queue_path = tmp_path / "queue.sqlite3"
    queue = GPTActionQueue(queue_path)
    run_id = queue.start_run()
    monkeypatch.setattr("builtins.input", lambda _prompt: "wrong-run-id")

    exit_code = main(["--queue-db", str(queue_path), "recover-active-run"])

    assert exit_code == 1
    assert queue.get_active_run_id() == run_id
    assert "no changes were made" in capsys.readouterr().out


def test_recover_active_run_command_marks_orphan_failed(capsys, tmp_path) -> None:
    queue_path = tmp_path / "queue.sqlite3"
    queue = GPTActionQueue(queue_path)
    run_id = queue.start_run()
    queue.enqueue(
        run_id=run_id,
        protocol_version="1",
        mode="SIMPLE_TEXT",
        lang_in="en",
        lang_out="zh",
        input_text="Recover",
        semantic_context={},
    )

    exit_code = main(["--queue-db", str(queue_path), "recover-active-run", "--yes"])

    assert exit_code == 0
    assert queue.get_active_run_id() is None
    output = capsys.readouterr().out
    assert '"status": "FAILED"' in output
    assert run_id in output


def test_status_command_outputs_run_active_not_worker_alive(capsys, tmp_path) -> None:
    queue_path = tmp_path / "queue.sqlite3"
    queue = GPTActionQueue(queue_path)
    queue.start_run()

    exit_code = main(["--queue-db", str(queue_path), "status"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_active"] is True
    assert "worker_alive" not in payload


def test_queue_cli_settings_priority(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "GPT_ACTION_QUEUE_DB=./data/dotenv.sqlite3\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GPT_ACTION_QUEUE_DB", "./data/system.sqlite3")

    from_environment = load_queue_cli_settings()
    explicit = GPTActionQueueCLISettings(queue_db="./data/explicit.sqlite3")

    assert from_environment.queue_db == str(
        (tmp_path / "data/system.sqlite3").resolve()
    )
    assert explicit.queue_db == "./data/explicit.sqlite3"


def test_invalidate_request_command_deletes_completed_cache(capsys, tmp_path) -> None:
    queue_path = tmp_path / "queue.sqlite3"
    queue = GPTActionQueue(queue_path)
    run_id = queue.start_run()
    queued = queue.enqueue(
        run_id=run_id,
        protocol_version="1",
        mode="LLM_BATCH",
        lang_in="en",
        lang_out="zh",
        input_text="prompt",
        semantic_context={},
    )
    item = queue.claim_batch(
        max_requests=1,
        max_serialized_response_chars=30000,
        claim_ttl_seconds=900,
    )["requests"][0]
    queue.submit_result(
        request_id=item["request_id"],
        claim_token=item["claim_token"],
        output_text="INVALID OUTPUT",
    )
    queue.complete_run(run_id)

    exit_code = main(
        [
            "--queue-db",
            str(queue_path),
            "invalidate-request",
            str(queued.request_id),
            "--yes",
        ]
    )

    assert exit_code == 0
    assert queue.get_completed_request(str(queued.request_id)) is None
    output = capsys.readouterr().out
    assert '"status": "INVALIDATED"' in output
    assert queued.request_id in output


def test_queue_cli_uses_pdf2zh_env_file_from_other_directory(
    monkeypatch, tmp_path
) -> None:
    config_dir = tmp_path / "config"
    launch_dir = tmp_path / "launch"
    config_dir.mkdir()
    launch_dir.mkdir()
    env_file = config_dir / ".env"
    env_file.write_text(
        "GPT_ACTION_QUEUE_DB=./data/from-external-env.sqlite3\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(launch_dir)
    monkeypatch.setenv("PDF2ZH_ENV_FILE", str(env_file))

    settings = load_queue_cli_settings()

    assert settings.queue_db == str(
        (config_dir / "data/from-external-env.sqlite3").resolve()
    )
