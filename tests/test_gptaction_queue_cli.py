from __future__ import annotations

import json

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
