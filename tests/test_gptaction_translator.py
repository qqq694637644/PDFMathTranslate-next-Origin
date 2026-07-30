from __future__ import annotations

import pickle
import threading
import time

import pytest
from pdf2zh_next.config.model import SettingsModel
from pdf2zh_next.config.translate_engine_model import TERM_EXTRACTION_ENGINE_METADATA
from pdf2zh_next.config.translate_engine_model import GPTActionSettings
from pdf2zh_next.translator.gptaction_queue import GPTActionQueue
from pdf2zh_next.translator.gptaction_queue import QueueItemTooLargeError
from pdf2zh_next.translator.translator_impl.gptaction import (
    GPTActionTranslationCanceledError,
)
from pdf2zh_next.translator.translator_impl.gptaction import GPTActionTranslator


def build_translator(tmp_path):
    queue_path = tmp_path / "queue.sqlite3"
    queue = GPTActionQueue(queue_path)
    run_id = queue.start_run()
    engine = GPTActionSettings(gptaction_queue_db=str(queue_path))
    engine._gptaction_run_id = run_id
    settings = SettingsModel(translate_engine_settings=engine)
    settings.translation.no_auto_extract_glossary = False
    translator = GPTActionTranslator(settings, None)
    return queue, run_id, settings, translator


def wait_for_pending(queue: GPTActionQueue, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queue.queue_status()["pending"]:
            return
        time.sleep(0.02)
    raise AssertionError("translator did not enqueue a request")


def test_health_check_and_llm_probe_do_not_enqueue(tmp_path) -> None:
    queue, _, _, translator = build_translator(tmp_path)
    translator.health_check()
    assert translator.do_llm_translate(None) is None
    assert translator.llm_translate(None) is None
    status = queue.queue_status()
    assert status["pending"] == 0
    assert status["claimed"] == 0
    assert status["completed"] == 0


def test_simple_translation_round_trip(tmp_path) -> None:
    queue, _, _, translator = build_translator(tmp_path)
    result: dict[str, object] = {}

    def translate() -> None:
        try:
            result["output"] = translator.translate("Hello")
        except Exception as exc:  # pragma: no cover - assertion reports the error
            result["error"] = exc

    thread = threading.Thread(target=translate)
    thread.start()
    wait_for_pending(queue)
    item = queue.claim_batch(
        max_requests=1,
        max_serialized_response_chars=30000,
        claim_ttl_seconds=900,
    )["requests"][0]
    assert item["mode"] == "SIMPLE_TEXT"
    queue.submit_result(
        request_id=item["request_id"],
        claim_token=item["claim_token"],
        output_text="你好",
    )
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result == {"output": "你好"}


def test_llm_batch_result_is_returned_verbatim(tmp_path) -> None:
    queue, _, _, translator = build_translator(tmp_path)
    result: dict[str, object] = {}
    prompt = '[{"id":0,"input":"Hello","layout_label":"text"}]'
    output = '[{"id":0,"output":"你好"}]'

    thread = threading.Thread(
        target=lambda: result.setdefault("output", translator.llm_translate(prompt))
    )
    thread.start()
    wait_for_pending(queue)
    item = queue.claim_batch(
        max_requests=1,
        max_serialized_response_chars=30000,
        claim_ttl_seconds=900,
    )["requests"][0]
    assert item["mode"] == "LLM_BATCH"
    queue.submit_result(
        request_id=item["request_id"],
        claim_token=item["claim_token"],
        output_text=output,
    )
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result["output"] == output


def test_cancel_event_stops_waiting_translator(tmp_path) -> None:
    queue, run_id, _, translator = build_translator(tmp_path)
    cancel_event = threading.Event()
    translator.bind_cancel_event(cancel_event)
    result: dict[str, object] = {}

    def translate() -> None:
        try:
            translator.translate("Cancel")
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=translate)
    thread.start()
    wait_for_pending(queue)
    cancel_event.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert isinstance(result["error"], GPTActionTranslationCanceledError)
    assert queue.queue_status()["status"] == "CANCELED"
    with queue._connect() as connection:
        row = connection.execute(
            "SELECT status FROM translation_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    assert row["status"] == "CANCELED"


def test_gptaction_is_not_available_for_term_extraction(tmp_path) -> None:
    engine_names = {
        metadata.translate_engine_type for metadata in TERM_EXTRACTION_ENGINE_METADATA
    }
    assert "GPTAction" not in engine_names

    queue_path = tmp_path / "queue.sqlite3"
    settings = SettingsModel(
        translate_engine_settings=GPTActionSettings(gptaction_queue_db=str(queue_path)),
        term_extraction_engine_settings=None,
    )
    settings.validate_settings()
    assert settings.translation.no_auto_extract_glossary is True
    assert settings.term_extraction_engine_settings is None


def test_explicit_queue_path_must_match_environment(monkeypatch, tmp_path) -> None:
    explicit_path = tmp_path / "explicit.sqlite3"
    environment_path = tmp_path / "environment.sqlite3"
    monkeypatch.setenv("GPT_ACTION_QUEUE_DB", str(environment_path))
    settings = GPTActionSettings(gptaction_queue_db=str(explicit_path))

    with pytest.raises(ValueError, match="queue path mismatch"):
        settings.validate_settings()


def test_active_run_id_survives_spawn_serialization(tmp_path) -> None:
    queue_path = tmp_path / "queue.sqlite3"
    run_id = GPTActionQueue(queue_path).start_run()
    engine = GPTActionSettings(gptaction_queue_db=str(queue_path))
    engine._gptaction_run_id = run_id
    settings = SettingsModel(translate_engine_settings=engine)

    restored = pickle.loads(pickle.dumps(settings))  # noqa: S301 - trusted test object

    assert restored.translate_engine_settings._gptaction_run_id == run_id


def test_response_size_environment_is_shared_with_translator(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("GPT_ACTION_MAX_SERIALIZED_RESPONSE_CHARS", "45000")
    settings = GPTActionSettings(gptaction_queue_db=str(tmp_path / "queue.sqlite3"))

    settings.validate_settings()

    assert settings.gptaction_max_serialized_response_chars == "45000"


def test_translator_rejects_oversized_request_before_waiting(tmp_path) -> None:
    queue_path = tmp_path / "queue.sqlite3"
    queue = GPTActionQueue(queue_path)
    run_id = queue.start_run()
    engine = GPTActionSettings(
        gptaction_queue_db=str(queue_path),
        gptaction_max_serialized_response_chars="1000",
    )
    engine._gptaction_run_id = run_id
    translator = GPTActionTranslator(
        SettingsModel(translate_engine_settings=engine),
        None,
    )

    with pytest.raises(QueueItemTooLargeError):
        translator.translate("x" * 2000)

    assert queue.queue_status()["pending"] == 0
