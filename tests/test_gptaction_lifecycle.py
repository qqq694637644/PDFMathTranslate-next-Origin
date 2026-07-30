from __future__ import annotations

import asyncio

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
