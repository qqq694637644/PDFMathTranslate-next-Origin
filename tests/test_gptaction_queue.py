from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
from pdf2zh_next.translator.gptaction_queue import ActiveRunExistsError
from pdf2zh_next.translator.gptaction_queue import GPTActionQueue
from pdf2zh_next.translator.gptaction_queue import GPTActionQueueError
from pdf2zh_next.translator.gptaction_queue import QueueItemTooLargeError
from pdf2zh_next.translator.gptaction_queue import canonical_fingerprint


def enqueue(queue: GPTActionQueue, run_id: str, text: str, mode: str = "SIMPLE_TEXT"):
    return queue.enqueue(
        run_id=run_id,
        protocol_version="1",
        mode=mode,
        lang_in="en",
        lang_out="zh",
        input_text=text,
        semantic_context={"custom_system_prompt": ""},
    )


def test_mode_aware_fingerprint() -> None:
    common = {
        "protocol_version": "1",
        "lang_in": "en",
        "lang_out": "zh",
        "input_text": "same input",
        "semantic_context": {},
    }
    simple = canonical_fingerprint(mode="SIMPLE_TEXT", **common)
    batch = canonical_fingerprint(mode="LLM_BATCH", **common)
    assert simple != batch


def test_run_phase_transitions_are_visible_in_queue_status(tmp_path) -> None:
    queue = GPTActionQueue(tmp_path / "queue.sqlite3")
    run_id = queue.start_run()

    assert queue.queue_status()["status"] == "PREPARING"

    enqueue(queue, run_id, "Hello")
    assert queue.queue_status()["status"] == "TRANSLATING"

    queue.update_run_phase(run_id, "FINALIZING")
    assert queue.queue_status()["status"] == "FINALIZING"

    queue.complete_run(run_id)
    assert queue.queue_status()["status"] == "COMPLETED"


def test_schema_version_one_is_migrated_with_preparing_phase(tmp_path) -> None:
    database_path = tmp_path / "queue.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_info (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                version INTEGER NOT NULL
            );
            INSERT INTO schema_info(singleton, version) VALUES (1, 1);
            CREATE TABLE translation_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            );
            INSERT INTO translation_runs(
                run_id, status, created_at, updated_at, completed_at
            ) VALUES ('run_old', 'ACTIVE', '2026-01-01', '2026-01-01', NULL);
            """
        )

    queue = GPTActionQueue(database_path)

    assert queue.queue_status()["status"] == "PREPARING"
    with sqlite3.connect(database_path) as connection:
        version = connection.execute(
            "SELECT version FROM schema_info WHERE singleton = 1"
        ).fetchone()[0]
        phase = connection.execute(
            "SELECT phase FROM translation_runs WHERE run_id = 'run_old'"
        ).fetchone()[0]
    assert version == 2
    assert phase == "PREPARING"


def test_single_active_run_and_completed_result_reuse(tmp_path) -> None:
    queue = GPTActionQueue(tmp_path / "queue.sqlite3")
    run_id = queue.start_run()
    with pytest.raises(ActiveRunExistsError):
        queue.start_run()

    queued = enqueue(queue, run_id, "Hello")
    claimed = queue.claim_batch(
        max_requests=8,
        max_serialized_response_chars=30000,
        claim_ttl_seconds=900,
    )
    assert claimed["run_id"] == run_id
    assert [item["request_id"] for item in claimed["requests"]] == [queued.request_id]

    item = claimed["requests"][0]
    completed = queue.submit_result(
        request_id=item["request_id"],
        claim_token=item["claim_token"],
        output_text="你好",
    )
    assert completed.status == "COMPLETED"

    idempotent = queue.submit_result(
        request_id=item["request_id"],
        claim_token=item["claim_token"],
        output_text="你好",
    )
    assert idempotent.status == "IDEMPOTENT"

    conflict = queue.submit_result(
        request_id=item["request_id"],
        claim_token=item["claim_token"],
        output_text="您好",
    )
    assert conflict.status == "CONFLICT"

    queue.complete_run(run_id)
    next_run = queue.start_run()
    reused = enqueue(queue, next_run, "Hello")
    assert reused.reused_completed is True
    assert reused.output_text == "你好"
    assert reused.request_id is None
    assert reused.reused_request_id == queued.request_id
    assert len(reused.fingerprint) == 64


def test_expired_claim_reuses_token_and_first_result_wins(tmp_path) -> None:
    queue = GPTActionQueue(tmp_path / "queue.sqlite3")
    run_id = queue.start_run()
    queued = enqueue(queue, run_id, "Translate me")

    first = queue.claim_batch(
        max_requests=1,
        max_serialized_response_chars=30000,
        claim_ttl_seconds=900,
    )["requests"][0]
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with sqlite3.connect(queue.database_path) as connection:
        connection.execute(
            "UPDATE translation_requests SET claimed_until = ? WHERE request_id = ?",
            (expired, queued.request_id),
        )

    second = queue.claim_batch(
        max_requests=1,
        max_serialized_response_chars=30000,
        claim_ttl_seconds=900,
    )["requests"][0]
    assert second["request_id"] == first["request_id"]
    assert second["claim_token"] == first["claim_token"]

    accepted = queue.submit_result(
        request_id=first["request_id"],
        claim_token=first["claim_token"],
        output_text="译文",
    )
    assert accepted.status == "COMPLETED"


def test_cancel_run_rejects_late_submission(tmp_path) -> None:
    queue = GPTActionQueue(tmp_path / "queue.sqlite3")
    run_id = queue.start_run()
    enqueue(queue, run_id, "Cancel me")
    item = queue.claim_batch(
        max_requests=1,
        max_serialized_response_chars=30000,
        claim_ttl_seconds=900,
    )["requests"][0]

    queue.cancel_run(run_id)
    submitted = queue.submit_result(
        request_id=item["request_id"],
        claim_token=item["claim_token"],
        output_text="too late",
    )
    assert submitted.status == "CANCELED"


def test_failed_run_open_request_rebinds_to_next_run(tmp_path) -> None:
    queue = GPTActionQueue(tmp_path / "queue.sqlite3")
    failed_run = queue.start_run()
    original = enqueue(queue, failed_run, "Recover me")
    queue.fail_run(failed_run)

    next_run = queue.start_run()
    rebound = enqueue(queue, next_run, "Recover me")
    assert rebound.request_id == original.request_id

    claimed = queue.claim_batch(
        max_requests=1,
        max_serialized_response_chars=30000,
        claim_ttl_seconds=900,
    )
    assert claimed["run_id"] == next_run
    assert claimed["requests"][0]["request_id"] == original.request_id


def test_old_claim_can_complete_after_run_failed(tmp_path) -> None:
    queue = GPTActionQueue(tmp_path / "queue.sqlite3")
    run_id = queue.start_run()
    enqueue(queue, run_id, "Finish after crash")
    item = queue.claim_batch(
        max_requests=1,
        max_serialized_response_chars=30000,
        claim_ttl_seconds=900,
    )["requests"][0]
    queue.fail_run(run_id)

    submitted = queue.submit_result(
        request_id=item["request_id"],
        claim_token=item["claim_token"],
        output_text="崩溃后完成",
    )
    assert submitted.status == "COMPLETED"


def test_concurrent_claimers_receive_distinct_requests(tmp_path) -> None:
    queue_path = tmp_path / "queue.sqlite3"
    queue = GPTActionQueue(queue_path)
    run_id = queue.start_run()
    enqueue(queue, run_id, "First")
    enqueue(queue, run_id, "Second")
    barrier = threading.Barrier(2)
    claimed_ids: list[str] = []

    def claim_one() -> None:
        local_queue = GPTActionQueue(queue_path)
        barrier.wait()
        batch = local_queue.claim_batch(
            max_requests=1,
            max_serialized_response_chars=30000,
            claim_ttl_seconds=900,
        )
        claimed_ids.append(batch["requests"][0]["request_id"])

    threads = [threading.Thread(target=claim_one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert len(set(claimed_ids)) == 2


def test_oversized_request_is_rejected_before_insertion(tmp_path) -> None:
    queue = GPTActionQueue(tmp_path / "queue.sqlite3")
    run_id = queue.start_run()

    with pytest.raises(QueueItemTooLargeError) as raised:
        queue.enqueue(
            run_id=run_id,
            protocol_version="1",
            mode="SIMPLE_TEXT",
            lang_in="en",
            lang_out="zh",
            input_text="x" * 2000,
            semantic_context={},
            max_serialized_response_chars=1000,
        )

    assert raised.value.required_chars > raised.value.max_chars
    assert "required_chars=" in str(raised.value)
    status = queue.queue_status()
    assert status["pending"] == 0
    assert status["claimed"] == 0


def test_existing_request_reports_required_size_and_can_be_retried(tmp_path) -> None:
    queue = GPTActionQueue(tmp_path / "queue.sqlite3")
    run_id = queue.start_run()
    queued = queue.enqueue(
        run_id=run_id,
        protocol_version="1",
        mode="SIMPLE_TEXT",
        lang_in="en",
        lang_out="zh",
        input_text="x" * 2000,
        semantic_context={},
        max_serialized_response_chars=5000,
    )

    with pytest.raises(QueueItemTooLargeError) as raised:
        queue.claim_batch(
            max_requests=1,
            max_serialized_response_chars=1000,
            claim_ttl_seconds=900,
        )

    assert raised.value.request_id == queued.request_id
    assert raised.value.required_chars > 1000
    claimed = queue.claim_batch(
        max_requests=1,
        max_serialized_response_chars=raised.value.required_chars,
        claim_ttl_seconds=900,
    )
    assert claimed["requests"][0]["request_id"] == queued.request_id


def test_recover_active_run_preserves_completed_and_releases_claims(tmp_path) -> None:
    queue = GPTActionQueue(tmp_path / "queue.sqlite3")
    run_id = queue.start_run()
    first = enqueue(queue, run_id, "Completed")
    second = enqueue(queue, run_id, "Open")
    claimed = queue.claim_batch(
        max_requests=2,
        max_serialized_response_chars=30000,
        claim_ttl_seconds=900,
    )["requests"]
    queue.submit_result(
        request_id=claimed[0]["request_id"],
        claim_token=claimed[0]["claim_token"],
        output_text="已完成",
    )

    summary = queue.get_active_run_summary()
    assert summary is not None
    assert summary["run_id"] == run_id
    recovered = queue.recover_active_run(run_id)
    assert recovered == {
        "pending": 0,
        "released_claimed": 1,
        "completed": 1,
    }
    assert queue.get_active_run_id() is None
    assert queue.queue_status()["status"] == "FAILED"

    with sqlite3.connect(queue.database_path) as connection:
        connection.row_factory = sqlite3.Row
        completed_row = connection.execute(
            "SELECT status, output_text FROM translation_requests WHERE request_id = ?",
            (first.request_id,),
        ).fetchone()
        open_row = connection.execute(
            "SELECT status, claimed_until FROM translation_requests WHERE request_id = ?",
            (second.request_id,),
        ).fetchone()
    assert dict(completed_row) == {"status": "COMPLETED", "output_text": "已完成"}
    assert dict(open_row) == {"status": "PENDING", "claimed_until": None}

    next_run = queue.start_run()
    rebound = enqueue(queue, next_run, "Open")
    assert rebound.request_id == second.request_id
    next_claim = queue.claim_batch(
        max_requests=1,
        max_serialized_response_chars=30000,
        claim_ttl_seconds=900,
    )["requests"][0]
    assert next_claim["request_id"] == second.request_id


def test_character_limit_defense_rejects_100000(tmp_path) -> None:
    queue = GPTActionQueue(tmp_path / "queue.sqlite3")
    run_id = queue.start_run()

    with pytest.raises(ValueError, match="between 1000 and 99999"):
        queue.enqueue(
            run_id=run_id,
            protocol_version="1",
            mode="SIMPLE_TEXT",
            lang_in="en",
            lang_out="zh",
            input_text="Hello",
            semantic_context={},
            max_serialized_response_chars=100000,
        )

    with pytest.raises(ValueError, match="between 1000 and 99999"):
        queue.claim_batch(
            max_requests=1,
            max_serialized_response_chars=100000,
            claim_ttl_seconds=900,
        )


def test_invalidate_completed_request_prevents_future_reuse(tmp_path) -> None:
    queue = GPTActionQueue(tmp_path / "queue.sqlite3")
    run_id = queue.start_run()
    queued = enqueue(queue, run_id, "Bad cached output")
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

    with pytest.raises(GPTActionQueueError, match="while a GPT Action run is active"):
        queue.invalidate_completed_request(queued.request_id)

    queue.complete_run(run_id)
    invalidated = queue.invalidate_completed_request(queued.request_id)
    assert invalidated["request_id"] == queued.request_id
    assert invalidated["mode"] == "SIMPLE_TEXT"
    assert len(invalidated["fingerprint"]) == 64
    assert queue.get_completed_request(queued.request_id) is None

    next_run = queue.start_run()
    replacement = enqueue(queue, next_run, "Bad cached output")
    assert replacement.reused_completed is False
    assert replacement.request_id != queued.request_id
