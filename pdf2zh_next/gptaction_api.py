from __future__ import annotations

import hmac
import logging
from typing import Literal

import uvicorn
from fastapi import Depends
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.security import HTTPBearer
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

from pdf2zh_next.translator.gptaction_queue import SCHEMA_VERSION
from pdf2zh_next.translator.gptaction_queue import GPTActionQueue
from pdf2zh_next.translator.gptaction_queue import QueueItemTooLargeError
from pdf2zh_next.translator.gptaction_queue import resolve_queue_db_path

logger = logging.getLogger(__name__)


class GPTActionAPISettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GPT_ACTION_",
        extra="ignore",
        validate_default=True,
    )

    api_key: str
    queue_db: str | None = None
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    claim_ttl_seconds: int = 1800
    max_requests: int = 8
    max_serialized_response_chars: int = 30000
    max_submit_chars: int = 60000
    max_output_chars: int = 30000

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 16:
            raise ValueError("GPT_ACTION_API_KEY must contain at least 16 characters")
        return cleaned

    @field_validator("queue_db")
    @classmethod
    def normalize_queue_db(cls, value: str | None) -> str:
        return str(resolve_queue_db_path(value))

    @field_validator("claim_ttl_seconds")
    @classmethod
    def validate_claim_ttl(cls, value: int) -> int:
        if value < 60:
            raise ValueError("claim_ttl_seconds must be at least 60")
        return value

    @field_validator("max_requests")
    @classmethod
    def validate_max_requests(cls, value: int) -> int:
        if not 1 <= value <= 32:
            raise ValueError("max_requests must be between 1 and 32")
        return value

    @field_validator(
        "max_serialized_response_chars",
        "max_submit_chars",
        "max_output_chars",
    )
    @classmethod
    def validate_size_limits(cls, value: int) -> int:
        if value < 1000:
            raise ValueError("GPT Action serialized size limits must be at least 1000")
        return value

    @field_validator("max_output_chars")
    @classmethod
    def validate_output_limit(cls, value: int) -> int:
        if value > 30000:
            raise ValueError("max_output_chars cannot exceed 30000")
        return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class QueueStatusResponse(StrictModel):
    run_id: str | None
    status: str
    pending: int
    claimed: int
    completed: int
    run_active: bool


class NextBatchRequest(StrictModel):
    max_requests: int | None = Field(default=None, ge=1, le=32)


class ActionRequestItem(StrictModel):
    request_id: str
    claim_token: str
    mode: Literal["LLM_BATCH", "SIMPLE_TEXT"]
    lang_in: str
    lang_out: str
    input: str


class NextBatchResponse(StrictModel):
    run_id: str | None
    requests: list[ActionRequestItem]


class SubmitResultItem(StrictModel):
    request_id: str = Field(min_length=1, max_length=128)
    claim_token: str = Field(min_length=1, max_length=256)
    output: str = Field(min_length=1, max_length=30000)


class SubmitBatchRequest(StrictModel):
    results: list[SubmitResultItem] = Field(min_length=1, max_length=32)


class SubmitItemResponse(StrictModel):
    request_id: str
    status: Literal["COMPLETED", "IDEMPOTENT", "CONFLICT", "CANCELED", "ERROR"]
    error: str | None = None


class SubmitBatchResponse(StrictModel):
    results: list[SubmitItemResponse]


def create_app(settings: GPTActionAPISettings | None = None) -> FastAPI:
    resolved_settings = settings or GPTActionAPISettings()
    queue = GPTActionQueue(resolved_settings.queue_db)
    bearer = HTTPBearer(auto_error=False)
    bearer_dependency = Depends(bearer)

    async def require_bearer(
        credentials: HTTPAuthorizationCredentials | None = bearer_dependency,
    ) -> None:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(
                credentials.credentials,
                resolved_settings.api_key,
            )
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    app = FastAPI(
        title="PDFMathTranslate GPT Actions Translator",
        version="1.0.0",
        description=(
            "Personal durable queue used by Custom GPT sessions to translate "
            "official BabelDOC requests."
        ),
    )
    app.state.settings = resolved_settings
    app.state.queue = queue

    @app.get(
        "/v1/actions/queue/status",
        response_model=QueueStatusResponse,
        operation_id="getQueueStatus",
        dependencies=[Depends(require_bearer)],
    )
    def get_queue_status() -> dict:
        return queue.queue_status()

    @app.post(
        "/v1/actions/batches/next",
        response_model=NextBatchResponse,
        operation_id="getNextBatch",
        dependencies=[Depends(require_bearer)],
    )
    def get_next_batch(payload: NextBatchRequest | None = None) -> dict:
        requested = payload.max_requests if payload else None
        maximum = min(
            requested or resolved_settings.max_requests, resolved_settings.max_requests
        )
        try:
            return queue.claim_batch(
                max_requests=maximum,
                max_serialized_response_chars=(
                    resolved_settings.max_serialized_response_chars
                ),
                claim_ttl_seconds=resolved_settings.claim_ttl_seconds,
            )
        except QueueItemTooLargeError as exc:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=str(exc),
            ) from exc

    @app.post(
        "/v1/actions/batches/submit",
        response_model=SubmitBatchResponse,
        operation_id="submitBatch",
        dependencies=[Depends(require_bearer)],
    )
    async def submit_batch(
        request: Request,
        payload: SubmitBatchRequest,
    ) -> dict:
        raw_body = await request.body()
        try:
            body_chars = len(raw_body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Request body must be UTF-8 JSON",
            ) from exc
        if body_chars > resolved_settings.max_submit_chars:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=(
                    "Serialized submit request exceeds limit: "
                    f"{body_chars} > {resolved_settings.max_submit_chars}"
                ),
            )

        results = []
        for item in payload.results:
            if len(item.output) > resolved_settings.max_output_chars:
                results.append(
                    {
                        "request_id": item.request_id,
                        "status": "ERROR",
                        "error": (
                            "output exceeds per-item limit: "
                            f"{len(item.output)} > {resolved_settings.max_output_chars}"
                        ),
                    }
                )
                continue
            submitted = queue.submit_result(
                request_id=item.request_id,
                claim_token=item.claim_token,
                output_text=item.output,
            )
            results.append(
                {
                    "request_id": submitted.request_id,
                    "status": submitted.status,
                    "error": submitted.error,
                }
            )
        return {"results": results}

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = GPTActionAPISettings()
    logger.info(
        "Starting GPT Actions API on %s:%s; queue=%s; schema=%s",
        settings.api_host,
        settings.api_port,
        settings.queue_db,
        SCHEMA_VERSION,
    )
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
