from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
from pdf2zh_next.translator.gptaction_queue import ActiveRunExistsError
from pdf2zh_next.translator.gptaction_queue import GPTActionQueue
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
