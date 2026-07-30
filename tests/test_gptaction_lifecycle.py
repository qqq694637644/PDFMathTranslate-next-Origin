from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pdf2zh_next import high_level
from pdf2zh_next.config.model import SettingsModel
from pdf2zh_next.config.translate_engine_model import GPTActionSettings
from pdf2zh_next.translator.gptaction_queue import GPTActionQueue


def build_settings(tmp_path) -> tuple[SettingsModel, GPTActionQueue]:
    queue_path = tmp_path / "queue.sqlite3"
    settings = SettingsModel(
        translate_engine_settings=GPTActionSettings(gptaction_queue_db=str(queue_path))
    )
    return settings, GPTActionQueue(queue_path)


def test_gptaction_subprocess_has_no_progress_idle_timeout(tmp_path) -> None:
    settings, _ = build_settings(tmp_path)
    assert high_level._subprocess_progress_timeout(settings) is None
    non_gptaction_settings = SimpleNamespace(
        translate_engine_settings=SimpleNamespace(translate_engine_type="Google")
    )
    assert high_level._subprocess_progress_timeout(non_gptaction_settings) == 30 * 60


def test_finish_event_marks_run_completed(monkeypatch, tmp_path) -> None:
    settings, queue = build_settings(tmp_path)
    pdf = tmp_path / "document.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    async def fake_translate(_settings, _file):
        yield {"type": "progress", "progress": 50}
        yield {"type": "finish", "translate_result": []}

    monkeypatch.setattr(high_level, "_translate_in_subprocess", fake_translate)

    async def collect_events():
        return [
            event async for event in high_level.do_translate_async_stream(settings, pdf)
        ]

    events = asyncio.run(collect_events())

    assert [event["type"] for event in events] == ["progress", "finish"]
    assert queue.queue_status()["status"] == "COMPLETED"


def test_closing_stream_marks_run_canceled(monkeypatch, tmp_path) -> None:
    settings, queue = build_settings(tmp_path)
    pdf = tmp_path / "document.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    async def fake_translate(_settings, _file):
        yield {"type": "progress", "progress": 10}
        await asyncio.sleep(60)

    monkeypatch.setattr(high_level, "_translate_in_subprocess", fake_translate)

    async def close_stream():
        stream = high_level.do_translate_async_stream(settings, pdf)
        first = await anext(stream)
        assert first["type"] == "progress"
        await stream.aclose()

    asyncio.run(close_stream())

    assert queue.queue_status()["status"] == "CANCELED"


def test_unexpected_worker_error_marks_run_failed(monkeypatch, tmp_path) -> None:
    settings, queue = build_settings(tmp_path)
    pdf = tmp_path / "document.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    async def fake_translate(_settings, _file):
        if False:
            yield None
        raise RuntimeError("worker failed")

    monkeypatch.setattr(high_level, "_translate_in_subprocess", fake_translate)

    async def consume_stream():
        async for _ in high_level.do_translate_async_stream(settings, pdf):
            pass

    try:
        asyncio.run(consume_stream())
    except RuntimeError as exc:
        assert str(exc) == "worker failed"
    else:  # pragma: no cover - protects the expected failure path
        raise AssertionError("worker failure was not propagated")

    assert queue.queue_status()["status"] == "FAILED"


def test_progress_events_expose_preparing_translating_and_finalizing(
    monkeypatch, tmp_path
) -> None:
    settings, queue = build_settings(tmp_path)
    pdf = tmp_path / "document.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    async def fake_translate(_settings, _file):
        yield {
            "type": "progress_start",
            "stage": "Parse PDF and Create Intermediate Representation",
        }
        run_id = queue.get_active_run_id()
        queued = queue.enqueue(
            run_id=run_id,
            protocol_version="1",
            mode="SIMPLE_TEXT",
            lang_in="en",
            lang_out="zh",
            input_text="Hello",
            semantic_context={},
        )
        yield {"type": "progress_update", "stage": "Translate Paragraphs"}
        item = queue.claim_batch(
            max_requests=1,
            max_serialized_response_chars=30000,
            claim_ttl_seconds=900,
        )["requests"][0]
        queue.submit_result(
            request_id=queued.request_id,
            claim_token=item["claim_token"],
            output_text="你好",
        )
        yield {"type": "progress_start", "stage": "Save PDF"}
        yield {"type": "finish", "translate_result": []}

    monkeypatch.setattr(high_level, "_translate_in_subprocess", fake_translate)

    async def consume_with_statuses():
        stream = high_level.do_translate_async_stream(settings, pdf)
        statuses = []
        event_types = []
        async for event in stream:
            event_types.append(event["type"])
            statuses.append(queue.queue_status()["status"])
        return event_types, statuses

    event_types, statuses = asyncio.run(consume_with_statuses())

    assert event_types == [
        "progress_start",
        "progress_update",
        "progress_start",
        "finish",
    ]
    assert statuses == ["PREPARING", "TRANSLATING", "FINALIZING", "COMPLETED"]


def test_fatal_unrepresentable_request_blocks_finish_and_fails_run(
    monkeypatch, tmp_path
) -> None:
    settings, queue = build_settings(tmp_path)
    settings.basic.debug = True
    pdf = tmp_path / "document.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    fatal_details = {
        "error_code": "GPT_ACTION_REQUEST_UNREPRESENTABLE",
        "request_id": "req_too_large",
        "mode": "LLM_BATCH",
        "fingerprint": "a" * 64,
        "required_chars": 120000,
        "configured_limit": 99999,
    }
    translator = SimpleNamespace(get_fatal_error=lambda: fatal_details)
    fake_config = SimpleNamespace(
        translator=translator,
        term_extraction_translator=None,
    )

    async def fake_babeldoc_translate(*, translation_config):
        assert translation_config is fake_config
        yield {"type": "finish", "translate_result": []}

    monkeypatch.setattr(high_level, "create_babeldoc_config", lambda *_: fake_config)
    monkeypatch.setattr(high_level, "babeldoc_translate", fake_babeldoc_translate)

    async def consume():
        events = []
        async for event in high_level.do_translate_async_stream(settings, pdf):
            events.append(event)
        return events

    with pytest.raises(
        high_level.GPTActionRequestUnrepresentableError,
        match="GPT_ACTION_REQUEST_UNREPRESENTABLE",
    ):
        asyncio.run(consume())

    assert queue.queue_status()["status"] == "FAILED"
