from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from pdf2zh_next.gptaction_api import GPTActionAPISettings
from pdf2zh_next.gptaction_api import create_app
from pydantic import field_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict


class OpenAPIExportSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    public_base_url: str = "https://translate.example.com"

    @field_validator("public_base_url")
    @classmethod
    def validate_public_base_url(cls, value: str) -> str:
        cleaned = value.strip().rstrip("/")
        if not cleaned.startswith("https://"):
            raise ValueError("PUBLIC_BASE_URL must use HTTPS")
        return cleaned


def export_openapi(*, output: Path, server_url: str | None = None) -> Path:
    explicit_values = {"public_base_url": server_url} if server_url is not None else {}
    export_settings = OpenAPIExportSettings(**explicit_values)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as temp_dir:
        settings = GPTActionAPISettings(
            api_key="openapi-export-placeholder-key",
            queue_db=str(Path(temp_dir) / "queue.sqlite3"),
        )
        schema = create_app(settings).openapi()

    schema["servers"] = [
        {
            "url": export_settings.public_base_url,
            "description": "Public HTTPS endpoint of pdf2zh-action-api",
        }
    ]
    output.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output.resolve()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Export the Custom GPT Actions OpenAPI schema"
    )
    parser.add_argument(
        "--output",
        default="openapi/gpt-actions.openapi.json",
        help="Output JSON path",
    )
    parser.add_argument(
        "--server-url",
        default=None,
        help=(
            "Public HTTPS base URL. Overrides PUBLIC_BASE_URL from the system "
            "environment or root .env file."
        ),
    )
    args = parser.parse_args(argv)
    print(
        export_openapi(
            output=Path(args.output),
            server_url=args.server_url,
        )
    )


if __name__ == "__main__":
    main()
