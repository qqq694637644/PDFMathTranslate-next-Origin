from __future__ import annotations

import argparse
import hmac
import logging
import os
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

from pdf2zh_next.env_file import pydantic_env_file_kwargs
from pdf2zh_next.env_file import read_env_file_values
from pdf2zh_next.env_file import resolve_env_file_path
from pdf2zh_next.env_file import resolve_path_from_env_file
from pdf2zh_next.translator.gptaction_queue import MAX_ACTION_PAYLOAD_CHARS
from pdf2zh_next.translator.gptaction_queue import SCHEMA_VERSION
from pdf2zh_next.translator.gptaction_queue import GPTActionQueue
from pdf2zh_next.translator.gptaction_queue import QueueItemTooLargeError
from pdf2zh_next.translator.gptaction_queue import resolve_queue_db_path

logger = logging.getLogger(__name__)
FORBIDDEN_EXAMPLE_API_KEYS = {"replace-with-a-long-random-key"}


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
    max_requests: int = 2
    max_serialized_response_chars: int = 30000
    max_submit_chars: int = 60000
    max_output_chars: int = 30000

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 16:
            raise ValueError("GPT_ACTION_API_KEY must contain at least 16 characters")
        if cleaned in FORBIDDEN_EXAMPLE_API_KEYS:
            raise ValueError(
                "GPT_ACTION_API_KEY still uses the public .env.example placeholder"
            )
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
        if not 1000 <= value <= MAX_ACTION_PAYLOAD_CHARS:
            raise ValueError(
                "GPT Action character limits must be between 1000 and 99999"
            )
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
    output: str = Field(min_length=1, max_length=MAX_ACTION_PAYLOAD_CHARS)


class SubmitBatchRequest(StrictModel):
    results: list[SubmitResultItem] = Field(min_length=1, max_length=32)


class SubmitItemResponse(StrictModel):
    request_id: str
    status: Literal["COMPLETED", "IDEMPOTENT", "CONFLICT", "CANCELED", "ERROR"]
    error: str | None = None


class SubmitBatchResponse(StrictModel):
    results: list[SubmitItemResponse]


def load_api_settings(
    *,
    env_file: str | None = None,
    **explicit_values,
) -> GPTActionAPISettings:
    resolved_env_file = resolve_env_file_path(env_file)
    if explicit_values.get("queue_db") is not None:
        explicit_values["queue_db"] = str(
            resolve_queue_db_path(explicit_values["queue_db"])
        )
    elif os.getenv("GPT_ACTION_QUEUE_DB"):
        explicit_values["queue_db"] = str(
            resolve_queue_db_path(os.environ["GPT_ACTION_QUEUE_DB"])
        )
    else:
        dotenv_queue_db = read_env_file_values(resolved_env_file).get(
            "GPT_ACTION_QUEUE_DB"
        )
        if dotenv_queue_db:
            explicit_values["queue_db"] = str(
                resolve_path_from_env_file(dotenv_queue_db, resolved_env_file)
            )
    return GPTActionAPISettings(
        **explicit_values,
        **pydantic_env_file_kwargs(resolved_env_file),
    )


def create_app(settings: GPTActionAPISettings | None = None) -> FastAPI:
    resolved_settings = settings or load_api_settings()
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
        openapi_extra={"x-openai-isConsequential": False},
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
        openapi_extra={"x-openai-isConsequential": False},
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


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf2zh-action-api",
        description="Run the personal GPT Actions translation sidecar.",
    )
    parser.add_argument("--api-key")
    parser.add_argument(
        "--env-file",
        help="Shared dotenv path. Overrides PDF2ZH_ENV_FILE and the current .env.",
    )
    parser.add_argument("--queue-db")
    parser.add_argument("--api-host")
    parser.add_argument("--api-port", type=int)
    parser.add_argument("--claim-ttl-seconds", type=int)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--max-serialized-response-chars", type=int)
    parser.add_argument("--max-submit-chars", type=int)
    parser.add_argument("--max-output-chars", type=int)
    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    args = build_cli_parser().parse_args(argv)
    env_file = args.env_file
    explicit_values = {
        key: value
        for key, value in vars(args).items()
        if key != "env_file" and value is not None
    }
    settings = load_api_settings(env_file=env_file, **explicit_values)
    resolved_env_file = resolve_env_file_path(env_file)
    logger.info(
        "Starting GPT Actions API on %s:%s; env_file=%s; queue=%s; schema=%s",
        settings.api_host,
        settings.api_port,
        resolved_env_file,
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
