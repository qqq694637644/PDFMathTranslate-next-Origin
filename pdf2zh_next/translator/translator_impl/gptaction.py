from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from threading import Event
from typing import Any

from pdf2zh_next.config.model import SettingsModel
from pdf2zh_next.translator.base_rate_limiter import BaseRateLimiter
from pdf2zh_next.translator.base_translator import BaseTranslator
from pdf2zh_next.translator.gptaction_queue import SCHEMA_VERSION
from pdf2zh_next.translator.gptaction_queue import GPTActionQueue
from pdf2zh_next.translator.gptaction_queue import QueueRequestCanceledError
from pdf2zh_next.translator.gptaction_queue import QueueWaitCanceledError

logger = logging.getLogger(__name__)


class GPTActionTranslationCanceledError(RuntimeError):
    """Raised when the parent application cancels a queued translation."""


class GPTActionTranslator(BaseTranslator):
    """Translator adapter that delegates work to one or more Custom GPT sessions."""

    name = "gptaction"
    model = "custom-gpt-actions"

    def __init__(
        self,
        settings: SettingsModel,
        rate_limiter: BaseRateLimiter | None,
    ):
        super().__init__(settings, rate_limiter)
        translator_settings = settings.translate_engine_settings
        if translator_settings.translate_engine_type != "GPTAction":
            raise ValueError("GPTActionTranslator requires GPTActionSettings")

        self.protocol_version = str(translator_settings.gptaction_protocol_version)
        self.poll_interval_seconds = float(
            translator_settings.gptaction_poll_interval_seconds
        )
        self.max_serialized_response_chars = int(
            translator_settings.gptaction_max_serialized_response_chars
        )
        self.queue = GPTActionQueue(translator_settings.gptaction_queue_db)
        self.run_id = getattr(translator_settings, "_gptaction_run_id", None)
        if not self.run_id:
            self.run_id = self.queue.get_active_run_id()
        if not self.run_id:
            raise RuntimeError(
                "No active GPT Action translation run. Start translation through "
                "PDFMathTranslate-next instead of constructing the translator directly."
            )
        self._cancel_event: Event | None = None
        self._semantic_context = self._build_semantic_context(settings)

    @staticmethod
    def _build_semantic_context(settings: SettingsModel) -> dict[str, Any]:
        glossary_entries: list[dict[str, str]] = []
        if settings.translation.glossaries:
            for raw_path in settings.translation.glossaries.split(","):
                value = raw_path.strip()
                if not value:
                    continue
                path = Path(value).expanduser()
                entry: dict[str, str] = {}
                try:
                    entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    entry["unavailable_path"] = str(path.resolve())
                glossary_entries.append(entry)
        return {
            "custom_system_prompt": settings.translation.custom_system_prompt or "",
            "glossaries": glossary_entries,
        }

    def bind_cancel_event(self, cancel_event: Event) -> None:
        self._cancel_event = cancel_event

    def health_check(self) -> None:
        self.queue.verify_database()
        self.queue.verify_schema()
        self.queue.verify_writable()
        self.queue.assert_active_run(self.run_id)
        logger.info(
            "GPT Action queue ready: %s (schema=%s, run=%s)",
            self.queue.database_path,
            SCHEMA_VERSION,
            self.run_id,
        )

    def _translate_mode(
        self,
        mode: str,
        text: str,
        *,
        ignore_cache: bool = False,
    ) -> str:
        self.translate_call_count += 1
        result = self.queue.enqueue(
            run_id=self.run_id,
            protocol_version=self.protocol_version,
            mode=mode,
            lang_in=self.lang_in,
            lang_out=self.lang_out,
            input_text=text,
            semantic_context=self._semantic_context,
            reuse_completed=not (self.ignore_cache or ignore_cache),
            max_serialized_response_chars=self.max_serialized_response_chars,
        )
        effective_request_id = result.request_id or result.reused_request_id
        logger.info(
            "GPT Action translation request: request_id=%s mode=%s "
            "fingerprint=%s reused_completed=%s",
            effective_request_id,
            mode,
            result.fingerprint,
            result.reused_completed,
        )
        if result.reused_completed:
            self.translate_cache_call_count += 1
            return str(result.output_text)
        if result.request_id is None:
            raise RuntimeError("GPT Action queue returned no request or cached output")
        try:
            output = self.queue.wait_for_result(
                result.request_id,
                cancel_event=self._cancel_event,
                poll_interval_seconds=self.poll_interval_seconds,
                run_id=self.run_id,
            )
            logger.info(
                "GPT Action translation result received: request_id=%s mode=%s "
                "fingerprint=%s",
                result.request_id,
                mode,
                result.fingerprint,
            )
            return output
        except (QueueRequestCanceledError, QueueWaitCanceledError) as exc:
            raise GPTActionTranslationCanceledError(str(exc)) from exc

    def translate(self, text, ignore_cache=False, rate_limit_params: dict = None):
        return self._translate_mode(
            "SIMPLE_TEXT",
            text,
            ignore_cache=ignore_cache,
        )

    def llm_translate(self, text, ignore_cache=False, rate_limit_params: dict = None):
        if text is None:
            return None
        return self._translate_mode(
            "LLM_BATCH",
            text,
            ignore_cache=ignore_cache,
        )

    def do_translate(self, text, rate_limit_params: dict = None):
        return self._translate_mode("SIMPLE_TEXT", text)

    def do_llm_translate(self, text, rate_limit_params: dict = None):
        if text is None:
            return None
        return self._translate_mode("LLM_BATCH", text)
